"""
Billing correctness: a subscription's plan grant is identified by (subscription, BILLING PERIOD), not by the exact
timestamp string of the period start. Two subscription.created/updated snapshots for the same subscription and the same
period but a second apart (different event ids, so event dedupe does not catch them) must grant once. A real renewal,
the next period, must still grant. Paddle is faked (conftest guard); no provider is called.
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry
from app.services.credits import credit_summary
from app.services.subscription_sync import sync_subscription
from tests.billing_helpers import now
from tests.test_billing import _new_org, _post
from tests.test_billing_level1 import _settings  # noqa: F401  (autouse: Paddle test settings)
from tests.test_entitlements import _ev

STARTER, GROWTH = 180 * 60, 480 * 60
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _period(start: datetime, days: int = 30):
    return start.strftime(FMT), (start + timedelta(days=days)).strftime(FMT)


def _base():
    """A period that contains now, fixed to the second so two events can differ by exactly what a test chooses."""
    return (now() - timedelta(days=1)).replace(microsecond=0)


def _grants(org):
    db = SessionLocal()
    try:
        return (
            db.query(CreditLedgerEntry)
            .filter(
                CreditLedgerEntry.organization_id == uuid.UUID(org),
                CreditLedgerEntry.kind == "plan_grant",
            )
            .order_by(CreditLedgerEntry.created_at)
            .all()
        )
    finally:
        db.close()


def _sub():
    return f"sub_{uuid.uuid4().hex[:12]}"


def _send(
    org,
    sub,
    price,
    start,
    *,
    kind="subscription.created",
    days=30,
    at="2026-10-01T00:00:00Z",
):
    return _post(_ev(kind, sub, org, price, period=_period(start, days), at=at))


def test_the_same_created_event_delivered_twice_grants_once():
    _c, org = _new_org()
    sub, start = _sub(), _base()
    evt = _ev("subscription.created", sub, org, "pri_starter", period=_period(start))
    _post(evt)
    _post(evt)  # identical event id: webhook dedupe
    rows = _grants(org)
    assert len(rows) == 1 and rows[0].seconds == STARTER


def test_distinct_event_ids_for_the_same_subscription_and_period_grant_once():
    _c, org = _new_org()
    sub, start = _sub(), _base()
    _send(org, sub, "pri_starter", start, at="2026-10-01T00:00:00Z")
    _send(org, sub, "pri_starter", start, at="2026-10-01T00:00:01Z")
    rows = _grants(org)
    assert len(rows) == 1 and rows[0].seconds == STARTER


def test_a_second_created_event_whose_period_start_is_a_second_off_does_not_grant_again():
    """The reported failure: live-period timestamps one second apart produced a second grant key."""
    _c, org = _new_org()
    sub, start = _sub(), _base()
    _send(org, sub, "pri_starter", start, at="2026-10-01T00:00:00Z")
    _send(
        org, sub, "pri_starter", start + timedelta(seconds=1), at="2026-10-01T00:00:01Z"
    )
    rows = _grants(org)
    assert len(rows) == 1 and rows[0].seconds == STARTER
    db = SessionLocal()
    try:
        assert credit_summary(db, uuid.UUID(org)).included_granted_seconds == STARTER
    finally:
        db.close()


def test_period_start_jitter_in_either_direction_and_on_updates_never_adds_credits():
    _c, org = _new_org()
    sub, start = _sub(), _base()
    _send(org, sub, "pri_starter", start, at="2026-10-01T00:00:00Z")
    for i, off in enumerate((-1, 1, 3600, -3600), start=1):
        _send(
            org,
            sub,
            "pri_starter",
            start + timedelta(seconds=off),
            kind="subscription.updated",
            at=f"2026-10-01T00:00:0{i}Z",
        )
    assert [r.seconds for r in _grants(org)] == [STARTER]


def test_an_upgrade_with_a_drifted_period_start_tops_up_once_to_the_growth_allowance():
    _c, org = _new_org()
    sub, start = _sub(), _base()
    _send(org, sub, "pri_starter", start, at="2026-10-01T00:00:00Z")
    _send(
        org,
        sub,
        "pri_growth",
        start + timedelta(seconds=1),
        kind="subscription.updated",
        at="2026-10-01T00:00:01Z",
    )
    _send(
        org,
        sub,
        "pri_growth",
        start + timedelta(seconds=2),
        kind="subscription.updated",
        at="2026-10-01T00:00:02Z",
    )
    rows = _grants(org)
    assert (
        sorted(r.seconds for r in rows) == [STARTER, GROWTH - STARTER]
        and sum(r.seconds for r in rows) == GROWTH
    )


def test_a_legitimate_renewal_still_grants_the_next_period():
    _c, org = _new_org()
    sub = _sub()
    first = (now() - timedelta(days=40)).replace(
        microsecond=0
    )  # first period already over, renewal is the live one
    p1 = _period(first)
    renewal_start = first + timedelta(days=30)
    _post(
        _ev(
            "subscription.created",
            sub,
            org,
            "pri_starter",
            period=p1,
            at="2026-09-01T00:00:00Z",
        )
    )
    # the first period has ended, so only the renewal's grant can exist now; seed the first grant directly
    db = SessionLocal()
    try:
        from app.db.models.billing import Subscription
        from app.services.credits import ensure_plan_grant

        s = (
            db.query(Subscription)
            .filter(Subscription.external_subscription_id == sub)
            .one()
        )
        s.current_period_start, s.current_period_end = first, first + timedelta(days=30)
        db.flush()
        ensure_plan_grant(db, s, now=first + timedelta(days=1))
        db.commit()
    finally:
        db.close()
    assert [r.seconds for r in _grants(org)] == [STARTER]
    _send(
        org,
        sub,
        "pri_starter",
        renewal_start,
        kind="subscription.updated",
        at="2026-10-30T00:00:00Z",
    )
    rows = _grants(org)
    assert len(rows) == 2 and [r.seconds for r in rows] == [
        STARTER,
        STARTER,
    ]  # one per billing period
    assert {r.period_start for r in rows} == {first, renewal_start}
    # and the renewal itself is idempotent, even replayed with a drifted start
    _send(
        org,
        sub,
        "pri_starter",
        renewal_start + timedelta(seconds=1),
        kind="subscription.updated",
        at="2026-10-30T00:00:05Z",
    )
    assert len(_grants(org)) == 2


def test_a_renewal_whose_start_overlaps_the_old_period_end_by_a_second_is_still_a_renewal():
    _c, org = _new_org()
    sub = _sub()
    first = (now() - timedelta(days=40)).replace(microsecond=0)
    old_end = (
        first + timedelta(days=30) + timedelta(seconds=1)
    )  # Paddle's end is a second AFTER the renewal's start
    renewal_start = first + timedelta(days=30)
    db = SessionLocal()
    try:
        from app.db.models.billing import Subscription
        from app.services.credits import ensure_plan_grant

        _post(
            _ev(
                "subscription.created",
                sub,
                org,
                "pri_starter",
                period=_period(first),
                at="2026-09-01T00:00:00Z",
            )
        )
        s = (
            db.query(Subscription)
            .filter(Subscription.external_subscription_id == sub)
            .one()
        )
        s.current_period_start, s.current_period_end = first, old_end
        db.flush()
        ensure_plan_grant(db, s, now=first + timedelta(days=1))
        db.commit()
    finally:
        db.close()
    _send(
        org,
        sub,
        "pri_starter",
        renewal_start,
        kind="subscription.updated",
        at="2026-10-30T00:00:00Z",
    )
    assert len(_grants(org)) == 2


def test_two_subscriptions_or_organizations_never_share_a_grant():
    _c, a = _new_org()
    _c, b = _new_org()
    start = _base()
    _send(a, _sub(), "pri_starter", start)
    _send(b, _sub(), "pri_starter", start + timedelta(seconds=1))
    assert [r.seconds for r in _grants(a)] == [STARTER] and [
        r.seconds for r in _grants(b)
    ] == [STARTER]


def test_concurrent_deliveries_with_different_event_ids_and_drifted_periods_grant_once():
    _c, org = _new_org()
    sub, start = _sub(), _base()
    _send(org, sub, "pri_starter", start, at="2026-10-01T00:00:00Z")
    barrier, errors = threading.Barrier(6), []

    def worker(i):
        data = {
            "id": sub,
            "status": "active",
            "custom_data": {"organization_id": org},
            "items": [
                {"status": "active", "quantity": 1, "price": {"id": "pri_starter"}}
            ],
            "current_billing_period": {
                "starts_at": (start + timedelta(seconds=i + 1)).strftime(FMT),
                "ends_at": _period(start)[1],
            },
        }
        db = SessionLocal()
        try:
            barrier.wait()
            sync_subscription(
                db, data, datetime(2026, 10, 1, 0, 0, i + 1, tzinfo=timezone.utc)
            )
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert [r.seconds for r in _grants(org)] == [STARTER]
