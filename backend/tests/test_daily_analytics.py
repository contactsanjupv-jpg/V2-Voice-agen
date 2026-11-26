"""Daily analytics report (Task 7): in-app only, once per organization per local day, the dashboard's own counting rules,
tenant scoped, idempotent under concurrency, and never emailed."""
import threading
import uuid
from datetime import datetime, timezone

import pytest

from app.api.v1.dashboard import _totals as dashboard_totals
from app.core import email as email_mod
from app.db.base import SessionLocal
from app.db.models.business import Business
from app.db.models.calls import Call, CallDirection
from app.db.models.crm import Lead
from app.db.models.notifications import Notification
from app.db.models.tenancy import OrgRole
from app.db.models.voice_agent import Agent
from app.services import daily_analytics as da
from app.services import notifications as svc
from app.services import owner_notifications as on
from tests.test_call_pipeline import _new_org as _pipe_org
from tests.test_call_pipeline import _seed
from tests.test_notifications import TestClientFor, _add_user, _owner_id, _rows, _url  # noqa: F401


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


NOW = utc(2026, 11, 24, 9, 0)  # Tue 09:00 UTC: past 08:00 in UTC; "yesterday" is Mon 2026-11-23


def _explode(*_a, **_k):
    raise AssertionError("an email was attempted for the daily report")


@pytest.fixture(autouse=True)
def _no_email_ever(monkeypatch):
    monkeypatch.setattr(on, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email", _explode)


def _clear_signup_limits():
    """Signup is rate limited per client; the suite resets it between tests, and tests that make many organizations reset it between signups."""
    import redis as redis_lib

    from app.config import get_settings

    r = redis_lib.from_url(get_settings().REDIS_URL, decode_responses=True)
    for key in r.scan_iter("ratelimit:*"):
        r.delete(key)


def _org(tz=None):
    _clear_signup_limits()
    _c, org = _pipe_org()
    _seed(org)
    if tz:
        db = SessionLocal()
        try:
            db.query(Business).filter_by(organization_id=uuid.UUID(org)).one().timezone = tz
            db.commit()
        finally:
            db.close()
    return org


def _call(org, at, seconds=60, direction=CallDirection.inbound):
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one()
        db.add(Call(organization_id=uuid.UUID(org), agent_id=agent.id, retell_call_id=f"call_{uuid.uuid4().hex[:10]}", direction=direction,
                    status="ended", started_at=at, duration_seconds=seconds))
        db.commit()
    finally:
        db.close()


def _lead(org, at):
    db = SessionLocal()
    try:
        db.add(Lead(organization_id=uuid.UUID(org), name="Pat", phone="+15550001111", reason="r", source="call", created_at=at))
        db.commit()
    finally:
        db.close()


def _report(org, now=NOW, **kw):
    db = SessionLocal()
    try:
        made = da.report_for(db, uuid.UUID(org), now, **kw)
        db.commit()
        return made
    finally:
        db.close()


def _reports(org, user_id=None):
    return [r for r in _rows(org, user_id) if r.kind == "daily_report"]


def _day(org):
    """Yesterday (Mon 11-23, UTC): 2 calls (60s + 90s), 1 lead. Plus noise that must not count."""
    _call(org, utc(2026, 11, 23, 10), 60)
    _call(org, utc(2026, 11, 23, 23, 59), 90)
    _lead(org, utc(2026, 11, 23, 12))
    _call(org, utc(2026, 11, 23, 11), 500, CallDirection.test)  # a test call is not customer traffic
    _call(org, utc(2026, 11, 24, 0, 0), 60)  # today
    _call(org, utc(2026, 11, 22, 23, 59), 60)  # the day before: only the comparison
    _lead(org, utc(2026, 11, 22, 12))
    _lead(org, utc(2026, 11, 24, 1))


# ------------------------------------------------------------------ what a report says
def test_the_report_counts_yesterdays_customer_calls_minutes_and_leads_and_compares_with_the_day_before():
    org = _org()
    _day(org)
    assert _report(org) is True
    rows = _reports(org)
    assert [(r.category, r.severity, r.link) for r in rows] == [("analytics", "info", "/dashboard")]
    assert rows[0].title == "Daily report: Mon, Nov 23"
    assert rows[0].body == "Mon, Nov 23: 2 calls (3 min), 1 new lead. The day before: 1 call."
    assert rows[0].payload == {"date": "2026-11-23", "calls": 2, "minutes": 3, "leads": 1, "previous_calls": 1}


def test_the_figures_are_the_dashboards_own_figures():
    org = _org()
    _day(org)
    db = SessionLocal()
    try:
        t = dashboard_totals(db, uuid.UUID(org), utc(2026, 11, 23), utc(2026, 11, 24))
        assert da.day_totals(db, uuid.UUID(org), utc(2026, 11, 23), utc(2026, 11, 24)) == (t.calls, t.call_seconds, t.leads) == (2, 150, 1)
    finally:
        db.close()


def test_a_quiet_day_and_a_day_with_only_test_calls_make_no_report():
    org = _org()
    assert _report(org) is False
    _call(org, utc(2026, 11, 23, 10), 300, CallDirection.test)
    assert _report(org) is False and _reports(org) == []


def test_singular_wording_and_a_lead_only_day():
    org = _org()
    _lead(org, utc(2026, 11, 23, 9))
    assert _report(org) is True
    assert _reports(org)[0].body.startswith("Mon, Nov 23: 0 calls, 1 new lead.")


# ------------------------------------------------------------------ when
def test_nothing_before_eight_local_and_the_local_day_decides_what_yesterday_is():
    org = _org("Asia/Kolkata")  # UTC+5:30
    _call(org, utc(2026, 11, 23, 5, 0))  # Mon 10:30 IST: Monday
    _call(org, utc(2026, 11, 23, 19, 0))  # Tue 00:30 IST: TUESDAY, not Monday
    assert _report(org, utc(2026, 11, 24, 2, 0)) is False  # 07:30 IST: too early
    assert _report(org, utc(2026, 11, 24, 3, 0)) is True  # 08:30 IST
    assert _reports(org)[0].payload["calls"] == 1 and _reports(org)[0].payload["date"] == "2026-11-23"


def test_a_missed_stretch_is_not_back_filled():
    org = _org()
    _call(org, utc(2026, 11, 23, 10))
    assert _report(org, utc(2026, 11, 26, 9, 0)) is False  # worker was down: only the 25th would be reported, and it was quiet
    assert _reports(org) == []


# ------------------------------------------------------------------ idempotency
def test_running_it_again_and_again_makes_one_report():
    org = _org()
    _day(org)
    assert _report(org) is True
    assert _report(org) is False and _report(org) is False
    assert len(_reports(org)) == 1


def test_the_hourly_pass_is_idempotent_and_concurrent_passes_make_one_report_per_recipient():
    org = _org()
    _day(org)
    _a, admin = _add_user(org, OrgRole.admin)

    def run():
        db = SessionLocal()
        try:
            da.report_pass(db, NOW)
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    run()
    db = SessionLocal()
    try:
        counts = {u: db.query(Notification).filter_by(organization_id=uuid.UUID(org), recipient_user_id=u, kind="daily_report").count()
                  for u in (_owner_id(org), admin)}
    finally:
        db.close()
    assert counts == {_owner_id(org): 1, admin: 1}


# ------------------------------------------------------------------ who, and isolation
def test_owners_and_admins_get_it_and_members_do_not():
    org = _org()
    _day(org)
    _a, admin = _add_user(org, OrgRole.admin)
    _m, member = _add_user(org, OrgRole.member)
    _report(org)
    assert len(_reports(org, _owner_id(org))) == 1 and len(_reports(org, admin)) == 1 and _reports(org, member) == []


def test_one_organizations_activity_never_enters_or_reaches_another():
    a, b = _org(), _org()
    _day(a)
    _call(b, utc(2026, 11, 23, 10))
    _report(a)
    _report(b)
    assert _reports(a)[0].payload["calls"] == 2 and _reports(b)[0].payload["calls"] == 1 and _reports(b)[0].payload["leads"] == 0
    items = TestClientFor(b).get(_url(b), params={"category": "analytics"}).json()["items"]
    assert [i["kind"] for i in items] == ["daily_report"] and "2 calls" not in items[0]["body"]
    assert TestClientFor(a).get(_url(a), params={"category": "analytics"}).json()["items"][0]["body"].startswith("Mon, Nov 23: 2 calls")


def test_one_failing_organization_does_not_stop_the_others(monkeypatch):
    good, bad = _org(), _org()
    _call(good, utc(2026, 11, 23, 10))
    _call(bad, utc(2026, 11, 23, 10))
    real = da.report_for

    def flaky(db, org_id, now=None, force=False):
        if str(org_id) == bad:
            raise RuntimeError("boom")
        return real(db, org_id, now, force)

    monkeypatch.setattr(da, "report_for", flaky)
    db = SessionLocal()
    try:
        da.report_pass(db, NOW)
    finally:
        db.close()
    assert len(_reports(good)) == 1 and _reports(bad) == []


# ------------------------------------------------------------------ policy and safety
def test_the_report_is_in_app_only_and_the_worker_runs_it():
    import inspect

    from app.workers import run_worker

    assert "daily_report" in svc.CATALOGUE and "daily_report" not in on.EMAIL_KINDS and on.EMAIL_KINDS == frozenset()
    category, _sev, link, _aud = svc.CATALOGUE["daily_report"]
    assert category == "analytics" and svc.safe_link(link) == link
    assert "daily_analytics.report_pass" in inspect.getsource(run_worker)
    src = inspect.getsource(da)
    assert "send_email" not in src and "smtp" not in src.lower() and "resend" not in src.lower()


def test_a_failure_creating_the_report_never_breaks_the_pass(monkeypatch):
    org = _org()
    _day(org)
    monkeypatch.setattr(svc, "_compose", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("compose broke")))
    assert _report(org) is False and _reports(org) == []


def test_report_text_is_ours_never_payload_text():
    org = _org()
    db = SessionLocal()
    try:
        svc.create(db, uuid.UUID(org), "daily_report", "x1", {"date": "<script>", "calls": "9; DROP", "leads": 1})
        svc.create(db, uuid.UUID(org), "daily_report", "x2", {"date": "2026-11-23", "calls": 1, "leads": 0, "minutes": 1, "previous_calls": -4})
        db.commit()
    finally:
        db.close()
    got = {r.dedupe_key: (r.title, r.body) for r in _reports(org)}
    assert got["x1"] == ("Daily report", "Your daily activity report is ready.")
    assert got["x2"] == ("Daily report: Mon, Nov 23", "Mon, Nov 23: 1 call (1 min), 0 new leads.")


# ------------------------------------------------------------------ batching: nobody beyond the first page is left behind
def _eligible(db, since, after=None):
    return da._eligible_batch(db, since, after)


def _pages(since):
    db = SessionLocal()
    try:
        seen, after = [], None
        while True:
            page = _eligible(db, since, after)
            if not page:
                return seen
            seen.extend(page)
            after = page[-1]
    finally:
        db.close()


def test_keyset_pages_cover_every_eligible_organization_in_order_without_repeats(monkeypatch):
    monkeypatch.setattr(da, "BATCH_SIZE", 2)
    quiet = _org()  # no activity at all
    only_test = _org()
    _call(only_test, utc(2026, 11, 23, 10), 60, CallDirection.test)  # a test call does not make an organization eligible
    stale = _org()
    _call(stale, utc(2026, 11, 1, 10))  # outside the 72h window
    eligible = [_org() for _ in range(5)]
    for i, org in enumerate(eligible):
        (_call if i % 2 else _lead)(org, utc(2026, 11, 23, 10))
    seen = _pages(NOW - da.LOOKBACK)
    assert seen == sorted(seen) and len(seen) == len(set(seen))  # strictly ascending: no repeats, no skips
    mine = [str(o) for o in seen if str(o) in set(eligible) | {quiet, only_test, stale}]
    assert mine == sorted(eligible, key=lambda o: uuid.UUID(o).int)  # all five, nobody ineligible


def test_a_pass_with_many_small_pages_reports_every_organization_and_a_rerun_adds_nothing(monkeypatch):
    monkeypatch.setattr(da, "BATCH_SIZE", 2)
    orgs = [_org() for _ in range(7)]  # four pages
    for org in orgs:
        _call(org, utc(2026, 11, 23, 10))
    db = SessionLocal()
    try:
        first = da.report_pass(db, NOW)
        again = da.report_pass(db, NOW)
    finally:
        db.close()
    assert first >= 7 and again == 0
    assert all(len(_reports(o)) == 1 for o in orgs)


def test_a_failure_in_an_early_page_never_strands_the_later_pages(monkeypatch):
    monkeypatch.setattr(da, "BATCH_SIZE", 2)
    orgs = sorted((_org() for _ in range(6)), key=lambda o: uuid.UUID(o).int)
    for org in orgs:
        _call(org, utc(2026, 11, 23, 10))
    bad = orgs[0]  # the very first organization of the very first page
    real = da.report_for

    def flaky(db, org_id, now=None, force=False):
        if str(org_id) == bad:
            raise RuntimeError("boom")
        return real(db, org_id, now, force)

    monkeypatch.setattr(da, "report_for", flaky)
    db = SessionLocal()
    try:
        da.report_pass(db, NOW)
    finally:
        db.close()
    assert _reports(bad) == [] and all(len(_reports(o)) == 1 for o in orgs[1:])


def test_concurrent_workers_paging_the_same_organizations_make_one_report_each(monkeypatch):
    monkeypatch.setattr(da, "BATCH_SIZE", 2)
    orgs = [_org() for _ in range(5)]
    for org in orgs:
        _call(org, utc(2026, 11, 23, 10))

    def run():
        db = SessionLocal()
        try:
            da.report_pass(db, NOW)
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(len(_reports(o)) == 1 for o in orgs)


def test_more_than_two_thousand_eligible_organizations_are_all_visited_exactly_once():
    from app.db.models.tenancy import Organization

    n = 2_050
    db = SessionLocal()
    ids = [uuid.uuid4() for _ in range(n)]
    try:
        db.add_all([Organization(id=i, name=f"bulk-{i.hex[:8]}") for i in ids])
        db.flush()
        db.add_all([Lead(organization_id=i, name="Pat", phone="+15550001111", reason="r", source="call", created_at=utc(2026, 11, 23, 12)) for i in ids])
        db.commit()
        visited = []
        real = da.report_for
        da.report_for = lambda d, org_id, now=None, force=False: visited.append(org_id) or False
        try:
            da.report_pass(db, NOW)
        finally:
            da.report_for = real
        mine = set(ids)
        got = [o for o in visited if o in mine]
        assert len(got) == n == len(set(got))  # all 2,050, each once, including everything past the old 2,000 cut-off
        assert visited == sorted(visited)  # in id order across all pages
        assert len(visited) > 2000 and set(visited[2000:]) <= mine | set(visited)  # the old cut-off no longer ends the pass
    finally:
        db.rollback()
        db.query(Organization).filter(Organization.id.in_(ids)).delete(synchronize_session=False)  # leads cascade
        db.commit()
        db.close()
