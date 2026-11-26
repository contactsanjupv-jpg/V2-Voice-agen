"""In-app notifications (Task 3): creation, idempotency, retrieval, read state, tenant isolation, authorization, concurrency, real-time."""
import asyncio
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.v1 import notifications as notifications_api
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.notifications import Notification, OwnerNotification
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.main import app
from app.services import notifications as svc
from app.services import owner_notifications as on
from tests.billing_helpers import new_org
from tests.fakes import FakePhone
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse: signs the Retell webhook the lead scenario posts)
from tests.test_owner_notifications import LEAD, _analyzed, _enforce, _live_org, _paused_setup

PW = "correct-horse-battery-staple"


# ------------------------------------------------------------------ helpers
def _org():
    client, org = new_org("inapp")
    return client, org


def _add_user(org, role, email_prefix=None):
    """A user with a known password in the organization. Returns (client logged in as them, user_id)."""
    email = f"{email_prefix or role.value}-{uuid.uuid4().hex[:8]}@inapp-test.com"
    db = SessionLocal()
    try:
        user = User(email=email, password_hash=hash_password(PW))
        db.add(user)
        db.flush()
        db.add(OrganizationMember(organization_id=uuid.UUID(org), user_id=user.id, role=role))
        db.commit()
        uid = user.id
    finally:
        db.close()
    client = TestClient(app)
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PW}).status_code == 200
    return client, uid


def _owner_id(org):
    db = SessionLocal()
    try:
        return db.query(OrganizationMember).filter(OrganizationMember.organization_id == uuid.UUID(org), OrganizationMember.role == OrgRole.owner).one().user_id
    finally:
        db.close()


def _make(org, kind="payment_failed", key=None, payload=None, when=None):
    db = SessionLocal()
    try:
        created = svc.create(db, uuid.UUID(org), kind, key or uuid.uuid4().hex, payload, created_at=when)
        db.commit()
        return created
    finally:
        db.close()


def _rows(org, user_id=None):
    db = SessionLocal()
    try:
        q = db.query(Notification).filter(Notification.organization_id == uuid.UUID(org))
        if user_id:
            q = q.filter(Notification.recipient_user_id == user_id)
        return q.order_by(Notification.seq).all()
    finally:
        db.close()


def _url(org, tail=""):
    return f"/api/v1/orgs/{org}/notifications{tail}"


# ------------------------------------------------------------------ creation and persistence
def test_a_notification_is_persisted_for_the_owner_with_title_body_link_and_category():
    client, org = _org()
    assert _make(org, "payment_failed", "k1") is True
    (row,) = _rows(org)
    assert (row.kind, row.category, row.severity, row.link, row.read_at) == ("payment_failed", "billing", "critical", "/dashboard/settings/billing", None)
    assert row.title == "Payment failed" and "payment method" in row.body and row.recipient_user_id == _owner_id(org)
    body = client.get(_url(org)).json()  # persistent: a fresh request (a later session) still returns it
    assert [i["title"] for i in body["items"]] == ["Payment failed"] and body["unread_count"] == 1
    assert body["items"][0]["is_read"] is False and body["items"][0]["read_at"] is None and body["items"][0]["created_at"]


def test_the_audience_is_fixed_at_creation_billing_alerts_go_to_owners_and_admins_leads_to_everyone():
    client, org = _org()
    admin, admin_id = _add_user(org, OrgRole.admin)
    member, member_id = _add_user(org, OrgRole.member)
    _make(org, "payment_failed", "p1")
    _make(org, "lead", "l1", {"lead_id": str(uuid.uuid4())})
    assert {(r.kind, r.recipient_user_id) for r in _rows(org)} == {
        ("payment_failed", _owner_id(org)), ("payment_failed", admin_id),
        ("lead", _owner_id(org)), ("lead", admin_id), ("lead", member_id),
    }
    assert [i["kind"] for i in member.get(_url(org)).json()["items"]] == ["lead"]  # a member never receives billing notifications
    assert {i["kind"] for i in admin.get(_url(org)).json()["items"]} == {"payment_failed", "lead"}


def test_an_unknown_kind_is_refused_and_the_callers_transaction_survives():
    _client, org = _org()
    db = SessionLocal()
    try:
        assert svc.create(db, uuid.UUID(org), "made_up", "x") is False
        assert svc.create(db, uuid.UUID(org), "credits_empty", "after") is True  # the same transaction is still usable
        db.commit()
    finally:
        db.close()
    assert [r.kind for r in _rows(org)] == ["credits_empty"]


def test_a_rolled_back_cause_leaves_no_notification():
    _client, org = _org()
    db = SessionLocal()
    try:
        svc.create(db, uuid.UUID(org), "payment_failed", "rb")
        db.rollback()
    finally:
        db.close()
    assert _rows(org) == []


def test_the_lead_text_shown_is_one_safe_line_and_nothing_else_of_the_customer():
    _client, org = _org()
    db = SessionLocal()
    try:
        from app.db.models.crm import Lead

        lead = Lead(organization_id=uuid.UUID(org), name="Dana\nPlumber\x07 <b>x</b>", phone="+15551230000", reason="SECRET REASON", summary="SECRET SUMMARY", source="call")
        db.add(lead)
        db.flush()
        svc.create(db, uuid.UUID(org), "lead", str(lead.id), {"lead_id": str(lead.id)})
        db.commit()
    finally:
        db.close()
    (row,) = _rows(org)
    assert "\n" not in row.body and "\x07" not in row.body and "SECRET" not in row.body and "+1555" not in row.body and "SECRET" not in json.dumps(row.payload)


# ------------------------------------------------------------------ deduplication and idempotency
def test_the_same_cause_twice_creates_one_notification_per_recipient():
    _client, org = _org()
    _add_user(org, OrgRole.admin)
    assert _make(org, "low_credits", "period-1") is True
    assert _make(org, "low_credits", "period-1") is False  # a redelivered webhook or a retried sweeper
    assert len(_rows(org)) == 2  # owner + admin, once each
    assert _make(org, "low_credits", "period-2") is True  # a different cause is a different notification
    assert len(_rows(org)) == 4


def test_the_same_dedupe_key_in_another_organization_is_independent():
    _c1, org1 = _org()
    _c2, org2 = _org()
    assert _make(org1, "payment_failed", "same") and _make(org2, "payment_failed", "same")


def test_concurrent_creation_of_the_same_cause_makes_exactly_one_notification():
    _client, org = _org()
    _add_user(org, OrgRole.admin)
    barrier = threading.Barrier(8)
    results: list = []

    def worker():
        db = SessionLocal()
        try:
            barrier.wait(timeout=10)
            created = svc.create(db, uuid.UUID(org), "payment_failed", "race")
            db.commit()
            results.append(created)
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(map(str, results)) == ["False"] * 7 + ["True"]  # one winner; the rest were quiet no-ops, never errors
    assert len(_rows(org)) == 2  # owner + admin: one each


# ------------------------------------------------------------------ retrieval, ordering, pagination
def test_newest_first_unread_count_unread_only_and_category_filter():
    client, org = _org()
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    _make(org, "payment_failed", "a", when=base)
    _make(org, "low_credits", "b", when=base + timedelta(minutes=1))
    _make(org, "credits_empty", "c", when=base + timedelta(minutes=2))
    page = client.get(_url(org)).json()
    assert [i["kind"] for i in page["items"]] == ["credits_empty", "low_credits", "payment_failed"] and page["unread_count"] == 3
    assert [i["kind"] for i in client.get(_url(org), params={"category": "billing"}).json()["items"]] == ["payment_failed"]
    first = page["items"][0]["id"]
    client.post(_url(org, f"/{first}/read"))
    only = client.get(_url(org), params={"unread_only": "true"}).json()
    assert [i["kind"] for i in only["items"]] == ["low_credits", "payment_failed"] and only["unread_count"] == 2
    assert len(client.get(_url(org)).json()["items"]) == 3  # history keeps the read ones


def test_pagination_walks_every_notification_once_with_no_gaps_or_repeats():
    client, org = _org()
    for i in range(7):
        _make(org, "low_credits", f"p{i}")
    seen, cursor = [], None
    for _ in range(5):
        params = {"limit": 3, **({"before": cursor} if cursor else {})}
        page = client.get(_url(org), params=params).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 7 and len(set(seen)) == 7 and cursor is None
    assert [r.id for r in reversed(_rows(org))] == [uuid.UUID(s) for s in seen]


def test_page_parameters_are_validated():
    client, org = _org()
    for params in ({"limit": 0}, {"limit": 51}, {"before": 0}, {"category": "weather"}, {"unread_only": "maybe"}):
        assert client.get(_url(org), params=params).status_code == 422, params
    assert client.post(_url(org, "/not-a-uuid/read")).status_code == 422


# ------------------------------------------------------------------ read state
def test_marking_one_read_sets_the_time_once_and_only_for_that_user():
    owner, org = _org()
    admin, _ = _add_user(org, OrgRole.admin)
    _make(org, "payment_failed", "r1")
    nid = owner.get(_url(org)).json()["items"][0]["id"]
    first = owner.post(_url(org, f"/{nid}/read"))
    assert first.status_code == 200 and first.json()["unread_count"] == 0
    stamp = owner.get(_url(org)).json()["items"][0]["read_at"]
    assert stamp
    assert owner.post(_url(org, f"/{nid}/read")).status_code == 200  # idempotent...
    assert owner.get(_url(org)).json()["items"][0]["read_at"] == stamp  # ...and the first read time is kept
    assert admin.get(_url(org)).json()["unread_count"] == 1  # the admin's own copy is untouched


def test_opening_the_list_never_marks_anything_read():
    client, org = _org()
    _make(org, "payment_failed", "o1")
    for _ in range(3):
        assert client.get(_url(org)).json()["unread_count"] == 1
        assert client.get(_url(org, "/state")).json()["unread_count"] == 1


def test_mark_all_read_marks_what_exists_and_leaves_later_arrivals_unread():
    client, org = _org()
    for i in range(3):
        _make(org, "low_credits", f"m{i}")
    _make(org, "payment_failed", "bill")
    done = client.post(_url(org, "/read-all"), params={"category": "billing"}).json()
    assert done == {"marked": 1, "unread_count": 3}  # only the category asked for
    done = client.post(_url(org, "/read-all")).json()
    assert done == {"marked": 3, "unread_count": 0}
    _make(org, "credits_empty", "later")
    assert client.get(_url(org, "/state")).json()["unread_count"] == 1
    assert client.post(_url(org, "/read-all")).json()["marked"] == 1
    assert client.post(_url(org, "/read-all")).json() == {"marked": 0, "unread_count": 0}  # idempotent


def test_concurrent_mark_all_and_new_arrivals_never_lose_a_notification():
    client, org = _org()
    for i in range(5):
        _make(org, "low_credits", f"c{i}")
    errors = []

    def mark():
        try:
            assert client.post(_url(org, "/read-all")).status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def arrive():
        try:
            for i in range(5):
                _make(org, "credits_empty", f"n{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=mark), threading.Thread(target=mark), threading.Thread(target=arrive)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert errors == []
    final = client.get(_url(org), params={"limit": 50}).json()
    assert len(final["items"]) == 10  # nothing lost
    assert final["unread_count"] == sum(1 for i in final["items"] if not i["is_read"])  # the count always matches the rows


# ------------------------------------------------------------------ tenant isolation and authorization
def test_another_tenant_sees_nothing_and_cannot_mark_or_probe_ids():
    a, org_a = _org()
    b, org_b = _org()
    _make(org_a, "payment_failed", "secret")
    nid = a.get(_url(org_a)).json()["items"][0]["id"]
    assert b.get(_url(org_b)).json() == {"items": [], "next_cursor": None, "unread_count": 0}
    for call in (lambda: b.get(_url(org_a)), lambda: b.get(_url(org_a, "/state")), lambda: b.post(_url(org_a, "/read-all")), lambda: b.post(_url(org_a, f"/{nid}/read")), lambda: b.get(_url(org_a, "/stream"))):
        assert call().status_code == 404  # another tenant's organization: the same answer as one that does not exist
    assert b.post(_url(org_b, f"/{nid}/read")).status_code == 404  # A's id through B's OWN organization: not found, nothing written
    assert b.post(_url(org_b, f"/{uuid.uuid4()}/read")).status_code == 404  # indistinguishable from an id that never existed
    assert a.get(_url(org_a)).json()["unread_count"] == 1  # untouched
    assert _rows(org_a)[0].read_at is None


def test_a_colleague_cannot_read_or_mark_someone_elses_notification_in_the_same_organization():
    owner, org = _org()
    admin, _ = _add_user(org, OrgRole.admin)
    _make(org, "payment_failed", "same-org")
    owners_id = next(i["id"] for i in owner.get(_url(org)).json()["items"])
    admins_id = next(i["id"] for i in admin.get(_url(org)).json()["items"])
    assert owners_id != admins_id  # one row per recipient
    assert admin.post(_url(org, f"/{owners_id}/read")).status_code == 404
    assert owner.get(_url(org)).json()["unread_count"] == 1


def test_a_removed_member_loses_access_immediately():
    owner, org = _org()
    admin, admin_id = _add_user(org, OrgRole.admin)
    _make(org, "payment_failed", "gone")
    assert admin.get(_url(org)).status_code == 200
    db = SessionLocal()
    try:
        db.query(OrganizationMember).filter(OrganizationMember.user_id == admin_id).delete()
        db.commit()
    finally:
        db.close()
    assert admin.get(_url(org)).status_code == 404


@pytest.mark.parametrize("method, tail", [("get", ""), ("get", "/state"), ("get", "/stream"), ("post", "/read-all"), ("post", f"/{uuid.uuid4()}/read")])
def test_every_route_requires_a_session(method, tail):
    _client, org = _org()
    assert getattr(TestClient(app), method)(_url(org, tail)).status_code == 401


# ------------------------------------------------------------------ deep links and the database constraints
@pytest.mark.parametrize("link", ["/dashboard", "/dashboard/leads", "/dashboard/settings/billing", "/dashboard#credits", "/dashboard/leads?status=new"])
def test_safe_links_are_accepted(link):
    assert svc.safe_link(link) == link


@pytest.mark.parametrize("link", ["https://evil.example/dashboard", "//evil.example", "/dashboardx/../../etc", "javascript:alert(1)", "/settings", "/dashboard/ x", "/dashboard\\evil", "/dashboard/leads\n", "http://localhost/dashboard", "", "/dashboard/" + "a" * 300])
def test_unsafe_links_are_refused(link):
    assert svc.safe_link(link) is None


def test_the_database_itself_refuses_a_bad_link_category_or_severity():
    _client, org = _org()
    base = dict(organization_id=uuid.UUID(org), recipient_user_id=_owner_id(org), kind="x", category="billing", severity="info", title="t", body="", dedupe_key="d", payload={})
    for bad in ({"link": "https://evil.example"}, {"link": "//evil.example"}, {"category": "weather"}, {"severity": "panic"}):
        db = SessionLocal()
        try:
            db.add(Notification(**{**base, "dedupe_key": uuid.uuid4().hex, **bad}))
            with pytest.raises(IntegrityError):
                db.commit()
        finally:
            db.rollback()
            db.close()


def test_every_catalogue_entry_is_valid():
    for kind, (category, severity, link, audience) in svc.CATALOGUE.items():
        assert category in __import__("app.db.models.notifications", fromlist=["CATEGORIES"]).CATEGORIES and severity in ("info", "warning", "critical")
        assert svc.safe_link(link) == link and audience


# ------------------------------------------------------------------ the existing producers now create in-app notifications
def test_a_captured_lead_notifies_in_app_and_queues_no_email(outbox):
    org, agent, number = _live_org()
    _analyzed(org, agent, number, LEAD)
    items = TestClientFor(org).get(_url(org)).json()["items"]
    assert [(i["kind"], i["category"], i["link"]) for i in items] == [("lead", "leads", "/dashboard/leads")] and "Dana Plumber" in items[0]["body"]
    db = SessionLocal()
    try:
        assert db.query(OwnerNotification).filter(OwnerNotification.organization_id == uuid.UUID(org)).count() == 0
    finally:
        db.close()
    assert outbox.sent == []


def TestClientFor(org):  # noqa: N802 - a client logged in as the organization's owner
    from tests.test_owner_notifications import _owner_email

    c = TestClient(app)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == _owner_email(org)).one()
        user.password_hash = hash_password(PW)
        db.commit()
    finally:
        db.close()
    assert c.post("/api/v1/auth/login", json={"email": _owner_email(org), "password": PW}).status_code == 200
    return c


def test_pausing_the_receptionist_notifies_in_app_once(monkeypatch, outbox):
    org, provider = _paused_setup(monkeypatch)
    _enforce(org, provider)
    _enforce(org, provider)  # a later pass finds nothing live
    items = TestClientFor(org).get(_url(org)).json()["items"]
    assert [(i["kind"], i["category"]) for i in items] == [("service_paused", "receptionist")] and "minutes are used up" in items[0]["body"]
    assert outbox.sent == [] and FakePhone is not None


# ------------------------------------------------------------------ real-time (Server-Sent Events)
def _membership_id(org, user_id):
    db = SessionLocal()
    try:
        return db.query(OrganizationMember).filter(OrganizationMember.organization_id == uuid.UUID(org), OrganizationMember.user_id == user_id).one().id
    finally:
        db.close()


async def _collect(gen, steps_before=None):
    out = []
    async for chunk in gen:
        out.append(chunk)
        if steps_before and steps_before(out):
            break
    return out


def test_the_stream_pushes_state_only_when_it_changes_and_keeps_alive_otherwise():
    client, org = _org()
    mid = _membership_id(org, _owner_id(org))
    now = [0.0]
    events = []

    async def sleep(_s):  # advances a fake clock and makes the world change between checks
        now[0] += 1
        if now[0] == 2:
            _make(org, "payment_failed", "live-1")  # a new notification arrives
        if now[0] == 4:
            client.post(_url(org, "/read-all"))  # the user reads it (e.g. in another tab)

    async def run():
        async for chunk in notifications_api.stream_events(mid, 1, 6, sleep=sleep, clock=lambda: now[0]):
            events.append(chunk)

    asyncio.run(run())
    text_ = "".join(events)
    assert events[0] == "retry: 5000\n\n"
    states = [json.loads(e.split("data: ")[1]) for e in events if e.startswith("event: state")]
    assert states == [{"latest_seq": 0, "unread_count": 0}, {"latest_seq": states[1]["latest_seq"], "unread_count": 1}, {"latest_seq": states[1]["latest_seq"], "unread_count": 0}]
    assert ": keepalive" in text_ and events[-1].startswith("event: closed") and '"refresh"' in events[-1]


def test_the_stream_closes_when_the_membership_ends():
    _client, org = _org()
    admin, admin_id = _add_user(org, OrgRole.admin)
    mid = _membership_id(org, admin_id)
    db = SessionLocal()
    try:
        db.query(OrganizationMember).filter(OrganizationMember.id == mid).delete()
        db.commit()
    finally:
        db.close()
    out = asyncio.run(_collect(notifications_api.stream_events(mid, 0, 60)))
    assert out[-1].startswith("event: closed") and "membership_ended" in out[-1]


def test_the_stream_route_serves_event_stream_with_no_caching_and_ends_after_its_maximum_age(monkeypatch):
    client, org = _org()
    monkeypatch.setattr(get_settings(), "NOTIFICATION_STREAM_MAX_SECONDS", 0)
    with client.stream("GET", _url(org, "/stream")) as resp:
        body = "".join(resp.iter_text())
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream") and resp.headers["cache-control"] == "no-store"
    assert "retry: 5000" in body and "event: closed" in body


def test_a_user_cannot_hold_unlimited_streams(monkeypatch):
    client, org = _org()
    monkeypatch.setattr(get_settings(), "NOTIFICATION_STREAM_MAX_PER_USER", 2)
    monkeypatch.setitem(notifications_api._open_streams, _owner_id(org), 2)
    assert client.get(_url(org, "/stream")).status_code == 429
    notifications_api._open_streams.pop(_owner_id(org), None)


# ------------------------------------------------------------------ housekeeping
def test_old_notifications_are_pruned_and_recent_ones_kept():
    _client, org = _org()
    _make(org, "payment_failed", "old", when=datetime.now(timezone.utc) - timedelta(days=200))
    _make(org, "payment_failed", "new")
    db = SessionLocal()
    try:
        assert svc.prune(db) >= 1
    finally:
        db.close()
    assert [r.dedupe_key for r in _rows(org)] == ["new"]


def test_deleting_an_organization_removes_its_notifications():
    _client, org = _org()
    _make(org, "payment_failed", "cascade")
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM organizations WHERE id = :i"), {"i": uuid.UUID(org)})
        db.commit()
    finally:
        db.close()
    assert _rows(org) == []


_ = on


# ------------------------------------------------------------------ one person, two organizations
def test_a_user_in_two_organizations_never_sees_or_changes_one_through_the_other():
    _, org_a = _org()
    _, org_b = _org()
    owner_a = _owner_id(org_a)
    db = SessionLocal()
    try:
        # the organization A owner also joins organization B as an admin
        db.add(OrganizationMember(organization_id=uuid.UUID(org_b), user_id=owner_a, role=OrgRole.admin))
        db.commit()
        assert svc.create(db, uuid.UUID(org_a), "low_credits", "two-org-a", {"balance_minutes": 3, "threshold_minutes": 10})
        db.commit()
        m_a = db.query(OrganizationMember).filter_by(organization_id=uuid.UUID(org_a), user_id=owner_a).one()
        m_b = db.query(OrganizationMember).filter_by(organization_id=uuid.UUID(org_b), user_id=owner_a).one()
        note = db.query(Notification).filter_by(organization_id=uuid.UUID(org_a), recipient_user_id=owner_a, dedupe_key="two-org-a").one()
        assert svc.unread_count(db, m_a) == 1 and svc.unread_count(db, m_b) == 0
        assert svc.list_page(db, m_b)["items"] == []
        assert svc.mark_read(db, m_b, note.id) is False
        assert svc.mark_all_read(db, m_b) == 0
        db.expire_all()
        assert db.get(Notification, note.id).read_at is None
        assert svc.mark_read(db, m_a, note.id) is True
    finally:
        db.close()


# ------------------------------------------------------------------ category validation (list and mark-all-read)
ALL_CATEGORIES = ["calls", "leads", "credits", "billing", "receptionist", "phone", "business", "analytics", "product"]


def _seed_every_category(org):
    """One unread notification per category for the owner (inserted directly: not every category has a producer yet)."""
    owner = _owner_id(org)
    db = SessionLocal()
    try:
        for n, cat in enumerate(ALL_CATEGORIES):
            db.add(Notification(
                organization_id=uuid.UUID(org), recipient_user_id=owner, kind="test_" + cat, category=cat, severity="info",
                title=f"{cat} title", body="body", link="/dashboard", dedupe_key=f"cat-{n}", payload={},
            ))
        db.commit()
    finally:
        db.close()


def test_the_api_category_type_is_exactly_the_database_category_list():
    import typing

    from app.db.models.notifications import CATEGORIES

    assert list(typing.get_args(notifications_api.Category)) == list(CATEGORIES) == ALL_CATEGORIES


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_listing_filters_to_each_valid_category(category):
    org = _org()[1]
    _seed_every_category(org)
    c = TestClientFor(org)
    r = c.get(_url(org), params={"category": category})
    assert r.status_code == 200
    assert [i["category"] for i in r.json()["items"]] == [category]
    assert len(c.get(_url(org)).json()["items"]) == len(ALL_CATEGORIES)  # no filter: everything


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_mark_all_read_filters_to_each_valid_category(category):
    org = _org()[1]
    _seed_every_category(org)
    c = TestClientFor(org)
    r = c.post(_url(org, "/read-all"), params={"category": category})
    assert r.status_code == 200
    assert r.json()["marked"] == 1 and r.json()["unread_count"] == len(ALL_CATEGORIES) - 1
    items = {i["category"]: i["is_read"] for i in c.get(_url(org)).json()["items"]}
    assert items.pop(category) is True and not any(items.values())  # only that category was marked


@pytest.mark.parametrize("bad", ["", "CALLS", "Calls", " calls", "calls ", "all", "unknown", "calls,leads", "calls;drop table notifications", "'", "%", "null", "../leads", "calls\n"])
def test_invalid_categories_are_rejected_everywhere_and_change_nothing(bad):
    org = _org()[1]
    _seed_every_category(org)
    c = TestClientFor(org)
    assert c.get(_url(org), params={"category": bad}).status_code == 422
    assert c.post(_url(org, "/read-all"), params={"category": bad}).status_code == 422
    assert c.get(_url(org, "/state")).json()["unread_count"] == len(ALL_CATEGORIES)  # the rejected read-all marked nothing
