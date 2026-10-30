
"""Past-due grace must not mean unlimited unbilled calls.

Inside the 3-day past-due grace the receptionist stays on, but only until purchased credit is spent
more than OVERAGE_GRACE_MINUTES below zero: the same cut-off an active subscription gets. Exactly at
the limit is still on; one second beyond it is off. No provider is contacted (FakePhone stands in for
Retell, and subscriptions are written straight to the test database the way the Paddle webhook leaves them).
"""
import uuid
from datetime import timedelta

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.services.credits import add_ledger_entry
from app.services.entitlement import enforce_entitlements, suspended_since
from tests.billing_helpers import add_usage, new_org, now, subscribe

MIN = 60
LIMIT = 30 * MIN  # OVERAGE_GRACE_MINUTES, pinned by the fixture below


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "OVERAGE_GRACE_MINUTES", 30)
    monkeypatch.setattr(s, "PAST_DUE_GRACE_DAYS", 3)


def _past_due_org(days_since_failure=1):
    """An org whose plan period has just ended unpaid: the included allowance is no longer spendable, so
    every call from here on is charged to purchased credit (which is zero unless the test tops up)."""
    _c, org = new_org()
    subscribe(org, start=now() - timedelta(days=31), end=now() - timedelta(days=days_since_failure))
    db = SessionLocal()
    try:
        sub = db.query(Subscription).filter_by(organization_id=uuid.UUID(org)).one()
        sub.status = "past_due"
        sub.status_changed_at = now() - timedelta(days=days_since_failure)
        db.commit()
    finally:
        db.close()
    return org


def _use(org, seconds):
    add_usage(org, seconds, at=now() - timedelta(hours=1))  # after the period ended


def _suspended_since(org):
    db = SessionLocal()
    try:
        return suspended_since(db, uuid.UUID(org), now())
    finally:
        db.close()


# ---------------- past_due, inside the grace ----------------

def test_past_due_within_the_cutoff_stays_on():
    org = _past_due_org()
    _use(org, 20 * MIN)  # 20 min below zero, limit is 30
    assert _suspended_since(org) is None


def test_past_due_beyond_the_cutoff_is_suspended_now():
    org = _past_due_org()
    _use(org, 31 * MIN)
    db = SessionLocal()
    try:
        at = now()
        assert suspended_since(db, uuid.UUID(org), at) == at  # begins now, same as an active org
    finally:
        db.close()


@pytest.mark.parametrize(
    "used, suspended",
    [(LIMIT - 1, False), (LIMIT, False), (LIMIT + 1, True)],
    ids=["1s-inside", "exactly-at-limit", "1s-beyond"],
)
def test_past_due_exact_boundary_is_strictly_more_than_the_limit(used, suspended):
    org = _past_due_org()
    _use(org, used)
    assert (_suspended_since(org) is not None) is suspended


@pytest.mark.parametrize(
    "used, suspended",
    [(40 * MIN, False), (40 * MIN + 1, True)],
    ids=["purchased-credit-exactly-at-limit", "purchased-credit-1s-beyond"],
)
def test_past_due_cutoff_uses_the_real_credit_balance_including_purchased_credit(used, suspended):
    org = _past_due_org()
    db = SessionLocal()
    try:
        add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=10 * MIN, idempotency_key=f"t:{org}")
        db.commit()
    finally:
        db.close()
    _use(org, used)  # balance = 10 min bought - used
    assert (_suspended_since(org) is not None) is suspended


def test_past_due_with_credit_left_stays_on():
    org = _past_due_org()
    db = SessionLocal()
    try:
        add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=60 * MIN, idempotency_key=f"t:{org}")
        db.commit()
    finally:
        db.close()
    _use(org, 45 * MIN)
    assert _suspended_since(org) is None


def test_past_due_after_the_grace_ends_is_still_suspended_from_the_grace_end_regardless_of_credit():
    org = _past_due_org(days_since_failure=4)  # 3-day grace ended a day ago
    db = SessionLocal()
    try:
        add_ledger_entry(db, organization_id=uuid.UUID(org), kind="topup", seconds=600 * MIN, idempotency_key=f"t:{org}")
        db.commit()
        sub = db.query(Subscription).filter_by(organization_id=uuid.UUID(org)).one()
        assert suspended_since(db, uuid.UUID(org), now()) == sub.status_changed_at + timedelta(days=3)
    finally:
        db.close()


def test_worker_pauses_a_past_due_org_beyond_the_cutoff_and_keeps_its_number():
    from tests.fakes import FakePhone
    from tests.test_call_pipeline import _seed

    org = _past_due_org()
    _agent, number = _seed(org)
    provider = FakePhone()
    provider.numbers[number] = {"nickname": "n", "agent": "agent_x"}
    db = SessionLocal()
    try:
        _use(org, LIMIT)  # at the limit: nothing happens
        result = enforce_entitlements(db, provider, organization_ids={uuid.UUID(org)})
        db.expire_all()
        assert result["suspended"] == 0 and not provider.unassigns
        assert db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one().status == AgentStatus.active

        _use(org, 2 * MIN)  # now beyond it
        result = enforce_entitlements(db, provider, organization_ids={uuid.UUID(org)})
        db.expire_all()
        assert db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one().status == AgentStatus.paused
        phone = db.query(PhoneNumber).filter_by(organization_id=uuid.UUID(org)).one()
        assert phone.agent_id is None and phone.status == PhoneNumberStatus.active  # paused, NOT released
        assert provider.unassigns and result["released"] == 0

        # Repeated sweeps over the rest of the grace period.
        # The agent remains paused, the number stays assigned to the account,
        # and the provider is not asked to unassign the same number again.
        unassigns = len(provider.unassigns)

        for later in (
            now() + timedelta(hours=1),
            now() + timedelta(days=1),
            now() + timedelta(days=1, hours=23),
        ):
            result = enforce_entitlements(
                db,
                provider,
                now=later,
                organization_ids={uuid.UUID(org)},
            )
            db.expire_all()

            assert result["released"] == 0 and not provider.releases
            assert (
                db.query(Agent)
                .filter_by(organization_id=uuid.UUID(org))
                .one().status == AgentStatus.paused
            )

            phone = db.query(PhoneNumber).filter_by(
                organization_id=uuid.UUID(org)
            ).one()

            assert phone.status == PhoneNumberStatus.active
            assert phone.agent_id is None

        assert len(provider.unassigns) == unassigns

    finally:
        db.close()


# ---------------- active subscriptions: behaviour unchanged ----------------

def _active_org_with_no_included_minutes(monkeypatch):
    """Starter patched to one included minute, so usage beyond it is charged to purchased credit."""
    from app.services.plans import PLANS, Plan

    monkeypatch.setitem(PLANS, "starter", Plan("starter", "Starter", 1, PLANS["starter"].features, 1))
    _c, org = new_org()
    subscribe(org)
    return org


@pytest.mark.parametrize(
    "used, suspended",
    [(1 * MIN + LIMIT - 1, False), (1 * MIN + LIMIT, False), (1 * MIN + LIMIT + 1, True)],
    ids=["1s-inside", "exactly-at-limit", "1s-beyond"],
)
def test_active_cutoff_boundary_is_unchanged(monkeypatch, used, suspended):
    org = _active_org_with_no_included_minutes(monkeypatch)
    add_usage(org, used)
    db = SessionLocal()
    try:
        at = now()
        result = suspended_since(db, uuid.UUID(org), at)
        assert result == (at if suspended else None)
    finally:
        db.close()


def test_active_with_allowance_left_is_untouched():
    _c, org = new_org()
    subscribe(org)  # real Starter allowance
    add_usage(org, 60 * MIN)
    assert _suspended_since(org) is None
