"""Billing and credit in-app notifications (Task 5): created in the same transaction as the change, once per real transition, to
owners and admins only, scoped to their organization, and never emailed."""
import uuid
from datetime import timedelta

import pytest

from app.config import get_settings
from app.core import email as email_mod
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.db.models.notifications import Notification
from app.db.models.tenancy import OrgRole
from app.services import notifications as svc
from app.services import owner_notifications as on
from app.services.billing_reconcile import ReconcileReport, _ensure_grants
from app.services.subscription_sync import sync_subscription
from tests.billing_helpers import new_org, now, subscribe
from tests.test_notifications import PW, TestClientFor, _add_user, _owner_id, _rows, _url  # noqa: F401
from tests.test_billing import _post as paddle_post
from tests.test_topups import _adjust, _adjustment, _complete, _paddle, _purchase, paddle  # noqa: F401  (fixtures: prices, fake Paddle)


def _explode(*_a, **_k):
    raise AssertionError("an email was attempted for a billing notification")


@pytest.fixture(autouse=True)
def _no_email_ever(monkeypatch):
    monkeypatch.setattr(on, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email_result", _explode)
    monkeypatch.setattr(email_mod, "send_email", _explode)
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


class Sub:
    """One organization and one Paddle subscription whose snapshots are applied like the webhook / reconciler do."""

    def __init__(self):
        _c, self.org = new_org("billnote")
        self.sub_id = f"sub_{uuid.uuid4().hex[:10]}"
        self.start = now() - timedelta(days=1)
        self.end = self.start + timedelta(days=30)
        self.tick = 0

    def apply(self, plan="starter", status="active", cancel_at=None, start=None, tick=None):
        if start is not None:
            self.start, self.end = start, start + timedelta(days=30)
        self.tick = tick if tick is not None else self.tick + 1
        snap = {
            "id": self.sub_id, "status": status, "custom_data": {"organization_id": self.org},
            "items": [{"status": "active", "quantity": 1, "price": {"id": {"starter": "pri_starter", "growth": "pri_growth"}[plan]}}],
            "current_billing_period": {"starts_at": self.start.isoformat(), "ends_at": self.end.isoformat()},
        }
        if cancel_at is not None:
            snap["scheduled_change"] = {"action": "cancel", "effective_at": cancel_at.isoformat()}
        db = SessionLocal()
        try:
            applied = sync_subscription(db, snap, self.start + timedelta(minutes=self.tick))
            db.commit()
            return applied
        finally:
            db.close()

    def kinds(self, user_id=None):
        return [r.kind for r in _rows(self.org, user_id)]


# ------------------------------------------------------------------ subscription lifecycle
def test_a_new_subscription_notifies_the_owner_once_in_billing_and_credits():
    s = Sub()
    assert s.apply("starter")
    rows = _rows(s.org)
    assert [(r.kind, r.category, r.link) for r in rows] == [
        ("subscription_started", "billing", "/dashboard/settings/billing"),
        ("plan_minutes_added", "credits", "/dashboard/settings/billing"),
    ]
    assert "Starter" in rows[0].title + rows[0].body
    assert rows[0].recipient_user_id == _owner_id(s.org)


def test_a_redelivered_snapshot_notifies_nobody_again():
    s = Sub()
    s.apply("starter", tick=1)
    before = s.kinds()
    for _ in range(3):
        s.apply("starter", tick=1)
        s.apply("starter", tick=2)
    assert s.kinds() == before


def test_a_stale_snapshot_creates_nothing():
    s = Sub()
    s.apply("starter", tick=5)
    s.apply("growth", tick=1)  # older than what is applied: ignored
    assert s.kinds() == ["subscription_started", "plan_minutes_added"]


def test_a_plan_change_says_from_and_to_and_the_period_is_not_announced_twice():
    s = Sub()
    s.apply("starter")
    s.apply("growth")
    s.apply("growth")
    assert s.kinds().count("plan_changed") == 1 and s.kinds().count("plan_minutes_added") == 1
    body = next(r.body for r in _rows(s.org) if r.kind == "plan_changed")
    assert "Starter" in body and "Growth" in body


def test_a_failed_payment_then_recovery_notifies_once_each():
    s = Sub()
    s.apply("starter")
    s.apply("starter", status="past_due")
    s.apply("starter", status="past_due")
    s.apply("starter", status="active")
    s.apply("starter", status="active")
    k = s.kinds()
    assert k.count("payment_failed") == 1 and k.count("payment_recovered") == 1
    assert k.index("payment_failed") < k.index("payment_recovered")


def test_a_scheduled_cancellation_and_the_final_cancel_notify_once_each():
    s = Sub()
    s.apply("starter")
    s.apply("starter", cancel_at=s.end)
    s.apply("starter", cancel_at=s.end)
    s.apply("starter", status="canceled", cancel_at=s.end)
    s.apply("starter", status="canceled")
    k = s.kinds()
    assert k.count("cancellation_scheduled") == 1 and k.count("subscription_canceled") == 1
    assert {r.kind: r.severity for r in _rows(s.org)}["subscription_canceled"] == "warning"


def test_a_renewal_announces_the_new_periods_minutes():
    s = Sub()
    s.apply("starter")
    s.apply("starter", start=s.end + timedelta(minutes=1))  # a genuinely new period
    assert s.kinds().count("plan_minutes_added") == 2


def test_the_reconciler_granting_minutes_notifies_once_even_when_run_again():
    _c, org = new_org("billnote")
    sub_id = subscribe(org, "starter", grant=False)
    for _ in range(3):
        db = SessionLocal()
        try:
            _ensure_grants(db, ReconcileReport(), apply=True, organization_ids={uuid.UUID(org)})
        finally:
            db.close()
    assert [r.kind for r in _rows(org)] == ["plan_minutes_added"] and sub_id


# ------------------------------------------------------------------ top-ups, refunds, chargebacks
def test_a_completed_topup_notifies_once_with_the_minutes_added(paddle):
    _c, org = new_org("billnote")
    topup_id, txn, seconds = _purchase(org)
    assert _complete(org, topup_id, txn) is True
    assert _complete(org, topup_id, txn) is False  # a redelivery
    rows = _rows(org)
    assert [(r.kind, r.category) for r in rows] == [("topup_credited", "credits")]
    assert f"{seconds // 60} minutes" in rows[0].body


def test_a_failed_topup_notifies_nobody(paddle):
    _c, org = new_org("billnote")
    _purchase(org)
    assert _rows(org) == []


def test_a_refund_and_a_chargeback_each_notify_once(paddle):
    _c, org = new_org("billnote")
    topup_id, txn, _seconds = _purchase(org)
    _complete(org, topup_id, txn)
    adj = _adjustment(txn, subtotal=1000)
    assert _adjust(adj) is True and _adjust(adj) is False
    cb = _adjustment(txn, action="chargeback")
    assert _adjust(cb) is True and _adjust(cb) is False
    k = [r.kind for r in _rows(org)]
    assert k == ["topup_credited", "refund_applied", "chargeback_applied"]


# ------------------------------------------------------------------ who sees what
def test_only_owners_and_admins_are_told_and_members_are_not():
    s = Sub()
    _client, admin_id = _add_user(s.org, OrgRole.admin)
    _client, member_id = _add_user(s.org, OrgRole.member)
    s.apply("starter")
    assert "subscription_started" in s.kinds(admin_id) and "subscription_started" in s.kinds(_owner_id(s.org))
    assert s.kinds(member_id) == []


def test_one_organizations_billing_events_never_reach_another():
    a, b = Sub(), Sub()
    a.apply("starter")
    a.apply("starter", status="past_due")
    b.apply("growth")
    assert "payment_failed" in a.kinds() and "payment_failed" not in b.kinds()
    items_b = TestClientFor(b.org).get(_url(b.org)).json()["items"]
    assert {i["kind"] for i in items_b} == {"subscription_started", "plan_minutes_added"}
    assert TestClientFor(b.org).get(_url(a.org)).status_code in (403, 404)


def test_the_api_serves_the_new_kinds_with_billing_and_credits_categories_and_filters():
    s = Sub()
    s.apply("starter")
    c = TestClientFor(s.org)
    assert [i["kind"] for i in c.get(_url(s.org), params={"category": "credits"}).json()["items"]] == ["plan_minutes_added"]
    assert [i["kind"] for i in c.get(_url(s.org), params={"category": "billing"}).json()["items"]] == ["subscription_started"]


# ------------------------------------------------------------------ safety
def test_no_new_kind_is_email_eligible_and_all_are_catalogued_in_app():
    new = {"subscription_started", "plan_changed", "cancellation_scheduled", "subscription_canceled", "payment_recovered",
           "refund_applied", "chargeback_applied", "plan_minutes_added", "topup_credited"}
    assert new <= set(svc.CATALOGUE) and not (new & on.EMAIL_KINDS) and on.EMAIL_KINDS == frozenset()
    for kind in new:
        category, _sev, link, audience = svc.CATALOGUE[kind]
        assert category in ("billing", "credits") and link == "/dashboard/settings/billing" and audience == svc.AUDIENCE_ADMINS


def test_a_notification_failure_never_blocks_the_billing_change(monkeypatch):
    monkeypatch.setattr(svc, "_compose", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("compose broke")))
    s = Sub()
    assert s.apply("starter") is True
    assert s.kinds() == []
    db = SessionLocal()
    try:
        assert db.query(Subscription).filter_by(external_subscription_id=s.sub_id).one().status == "active"
    finally:
        db.close()


def test_a_rolled_back_change_leaves_no_notification():
    s = Sub()
    snap = {
        "id": s.sub_id, "status": "active", "custom_data": {"organization_id": s.org},
        "items": [{"status": "active", "quantity": 1, "price": {"id": "pri_starter"}}],
        "current_billing_period": {"starts_at": s.start.isoformat(), "ends_at": s.end.isoformat()},
    }
    db = SessionLocal()
    try:
        assert sync_subscription(db, snap, s.start)
        db.rollback()
    finally:
        db.close()
    assert s.kinds() == [] and s.sub_id


def test_message_text_comes_from_our_tables_never_from_payload_text():
    org = new_org("billnote")[1]
    db = SessionLocal()
    try:
        svc.create(db, uuid.UUID(org), "plan_changed", "x1", {"plan_id": "<script>alert(1)</script>", "previous_plan_id": "growth\nBcc: x"})
        svc.create(db, uuid.UUID(org), "topup_credited", "x2", {"minutes": "12; DROP TABLE"})
        svc.create(db, uuid.UUID(org), "refund_applied", "x3", {"minutes": -5})
        svc.create(db, uuid.UUID(org), "topup_credited", "x4", {"minutes": 90})
        db.commit()
    finally:
        db.close()
    rows = {r.dedupe_key: r for r in _rows(org)}
    assert rows["x1"].body == "Your plan was changed."
    assert "DROP" not in rows["x2"].body and rows["x2"].body == "Your top-up was added to your balance."
    assert rows["x3"].body == "A refund was applied to a purchase."
    assert rows["x4"].body == "90 minutes were added to your balance."
    assert all(isinstance(r, Notification) for r in rows.values())


def test_a_subscription_first_seen_in_a_non_live_state_is_not_announced_as_started():
    s = Sub()
    s.apply("starter", status="canceled")
    assert "subscription_started" not in s.kinds()
    p = Sub()
    p.apply("starter", status="past_due")
    assert "subscription_started" not in p.kinds()


# ------------------------------------------------------------------ refund approval lifecycle (pending / approved / rejected)
def _adj_event(adj, event_type):
    return {
        "event_id": f"evt_{uuid.uuid4().hex}", "event_type": event_type, "occurred_at": "2026-10-11T10:00:00.000000Z",
        "notification_id": f"ntf_{uuid.uuid4().hex}", "data": adj,
    }


def _refund_state(org):
    from app.db.models.billing import CreditLedgerEntry, TopupPurchase

    db = SessionLocal()
    try:
        row = db.query(TopupPurchase).filter_by(organization_id=uuid.UUID(org)).one()
        ledger = sorted(e.kind for e in db.query(CreditLedgerEntry).filter_by(organization_id=uuid.UUID(org)).all())
        return row.refunded_minor, row.clawed_back_seconds, ledger
    finally:
        db.close()


@pytest.mark.parametrize("status", ["pending_approval", "rejected", "reversed", "", None])
def test_a_refund_that_is_not_approved_changes_nothing_and_notifies_nobody(paddle, status):
    _c, org = new_org("billnote")
    topup_id, txn, _s = _purchase(org)
    _complete(org, topup_id, txn)
    before = _refund_state(org)
    adj = _adjustment(txn, subtotal=1000)
    adj["status"] = status
    assert _adjust(adj) is False
    assert "refund_applied" not in [r.kind for r in _rows(org)] and _refund_state(org) == before


def test_a_refund_pending_then_approved_notifies_exactly_once_on_approval(paddle):
    _c, org = new_org("billnote")
    topup_id, txn, _s = _purchase(org)
    _complete(org, topup_id, txn)
    before = _refund_state(org)
    adj = _adjustment(txn, subtotal=1000)

    adj["status"] = "pending_approval"
    assert paddle_post(_adj_event(adj, "adjustment.created")).status_code == 204
    assert paddle_post(_adj_event(adj, "adjustment.updated")).status_code == 204  # still pending
    assert [r.kind for r in _rows(org)] == ["topup_credited"] and _refund_state(org) == before  # no money moved, nobody told

    adj["status"] = "approved"
    assert paddle_post(_adj_event(adj, "adjustment.updated")).status_code == 204  # the approval arrives as an UPDATE
    assert [r.kind for r in _rows(org)] == ["topup_credited", "refund_applied"]
    refunded, clawed, ledger = _refund_state(org)
    assert refunded == 1000 and clawed > 0 and ledger.count("refund") == 1

    for event_type in ("adjustment.updated", "adjustment.created", "adjustment.updated"):  # redeliveries, new event ids
        assert paddle_post(_adj_event(adj, event_type)).status_code == 204
    assert [r.kind for r in _rows(org)] == ["topup_credited", "refund_applied"]
    assert _refund_state(org) == (refunded, clawed, ledger)


def test_a_refund_pending_then_rejected_never_notifies_and_a_later_approval_of_another_still_does(paddle):
    _c, org = new_org("billnote")
    topup_id, txn, _s = _purchase(org)
    _complete(org, topup_id, txn)
    before = _refund_state(org)
    adj = _adjustment(txn, subtotal=1000)

    adj["status"] = "pending_approval"
    assert paddle_post(_adj_event(adj, "adjustment.created")).status_code == 204
    adj["status"] = "rejected"
    assert paddle_post(_adj_event(adj, "adjustment.updated")).status_code == 204
    assert paddle_post(_adj_event(adj, "adjustment.updated")).status_code == 204
    assert [r.kind for r in _rows(org)] == ["topup_credited"] and _refund_state(org) == before

    other = _adjustment(txn, subtotal=500)  # a different refund request, approved
    other["status"] = "approved"
    assert paddle_post(_adj_event(other, "adjustment.updated")).status_code == 204
    assert [r.kind for r in _rows(org)] == ["topup_credited", "refund_applied"]


def test_a_refund_of_something_that_is_not_a_topup_gives_no_credit_notification(paddle):
    _c, org = new_org("billnote")
    adj = _adjustment("txn_subscription_payment", subtotal=1000)
    adj["status"] = "approved"
    assert paddle_post(_adj_event(adj, "adjustment.updated")).status_code == 204
    assert _rows(org) == []
