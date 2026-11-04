"""
Paddle delivers at-least-once. The SAME event (same event_id, same bytes) arriving twice - or eight times at once - must not
grant credits twice, move subscription state twice, or fire its side effects twice. Bad or stale signatures must change
nothing, and must not "use up" an event id that Paddle will legitimately retry. Real Postgres, real HTTP layer.
"""
import json
import threading
import time
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import CreditLedgerEntry, Subscription
from app.db.models.platform import WebhookEvent
from app.webhooks import paddle as paddle_webhook
from tests.billing_helpers import live_period
from tests.test_billing import _event as _event_fixed_period
from tests.test_billing import _new_org, _post, _sign
from tests.test_topups import _balance, _purchase, _txn, paddle  # noqa: F401  (fixture used by name)
from tests.test_topups import _adjustment, _paddle  # noqa: F401  (autouse settings fixture)


def _event(*args, **kwargs):
    return _event_fixed_period(*args, period=live_period(), **kwargs)  # a period that contains today, so the plan grant is created


def _body(event: dict) -> bytes:
    return json.dumps(event).encode()


def _deliver(body: bytes, signature=None):
    """Post exactly these bytes. A fresh, valid signature per attempt (Paddle re-signs each retry) unless told otherwise."""
    return _post(None, signature=_sign(body) if signature is None else signature, body=body)


def _count(model, **filters):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**filters).count()
    finally:
        db.close()


def _sub(sub_id):
    db = SessionLocal()
    try:
        row = db.query(Subscription).filter_by(external_subscription_id=sub_id).one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _grants(org):
    return _count(CreditLedgerEntry, organization_id=uuid.UUID(org))


@pytest.fixture
def spies(monkeypatch):
    """Count how often the webhook applies a subscription snapshot and fires its post-commit side effects."""
    calls = {"sync": 0, "after": 0}
    real_sync, real_after = paddle_webhook._sync_subscription, paddle_webhook.after_subscription_applied

    def sync(*a, **k):
        calls["sync"] += 1
        return real_sync(*a, **k)

    def after(*a, **k):
        calls["after"] += 1
        return real_after(*a, **k)

    monkeypatch.setattr(paddle_webhook, "_sync_subscription", sync)
    monkeypatch.setattr(paddle_webhook, "after_subscription_applied", after)
    return calls


# ---------------- the same subscription event, twice ----------------

def test_the_exact_same_subscription_event_delivered_twice_changes_nothing_the_second_time(spies):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    event = _event("subscription.created", sub_id, org, event_id=f"evt_{uuid.uuid4().hex}")
    body = _body(event)

    assert _deliver(body).status_code == 204
    state_after_first = _sub(sub_id)
    grants_after_first = _grants(org)
    assert grants_after_first >= 1  # the plan's included credit was granted once
    assert spies == {"sync": 1, "after": 1}

    assert _deliver(body).status_code == 204  # same event_id, same payload, a fresh valid signature
    second = _sub(sub_id)

    assert _grants(org) == grants_after_first  # no second grant
    assert _count(Subscription, organization_id=uuid.UUID(org)) == 1
    assert _count(WebhookEvent, external_event_id=event["event_id"]) == 1
    assert (second.status, second.plan_id, second.status_changed_at, second.last_event_at, second.updated_at) == (
        state_after_first.status, state_after_first.plan_id, state_after_first.status_changed_at,
        state_after_first.last_event_at, state_after_first.updated_at,
    )
    assert spies == {"sync": 1, "after": 1}  # the duplicate triggered no state sync and no side effects


def test_replaying_an_old_event_after_the_subscription_moved_on_changes_nothing(spies):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    created = _body(_event("subscription.created", sub_id, org, at="2026-10-01T00:00:00.000000Z"))
    canceled = _body(_event("subscription.canceled", sub_id, org, status="canceled", at="2026-10-05T00:00:00.000000Z"))
    assert _deliver(created).status_code == 204
    assert _deliver(canceled).status_code == 204
    grants, before = _grants(org), _sub(sub_id)
    assert before.status == "canceled"

    assert _deliver(created).status_code == 204  # Paddle retries the OLD created event
    assert _deliver(canceled).status_code == 204

    after = _sub(sub_id)
    assert (after.status, after.status_changed_at) == (before.status, before.status_changed_at)
    assert _grants(org) == grants
    assert spies["sync"] == 2  # the two first deliveries only


def test_eight_simultaneous_deliveries_of_one_event_apply_it_exactly_once(spies):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    event = _event("subscription.created", sub_id, org)
    body = _body(event)
    statuses, lock = [], threading.Lock()

    def go():
        code = _deliver(body).status_code
        with lock:
            statuses.append(code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert statuses == [204] * 8  # every delivery is acknowledged: one applied, seven recognised as duplicates
    assert _count(WebhookEvent, external_event_id=event["event_id"]) == 1
    assert _count(Subscription, organization_id=uuid.UUID(org)) == 1
    one_grant = _grants(org)
    assert _deliver(body).status_code == 204  # and a later retry is still a no-op
    assert _grants(org) == one_grant and _count(Subscription, organization_id=uuid.UUID(org)) == 1
    assert spies == {"sync": 1, "after": 1}


# ---------------- top-ups and refunds ----------------

def test_a_completed_top_up_event_delivered_twice_grants_credit_once(paddle):  # noqa: F811
    _c, org = _new_org()
    topup_id, txn, seconds = _purchase(org, 2500)
    body = _body({
        "event_id": f"evt_{uuid.uuid4().hex}", "event_type": "transaction.completed",
        "occurred_at": "2026-10-02T00:00:00.000000Z", "data": _txn(org, topup_id, txn, 2500),
    })
    assert _balance(org) == 0
    assert _deliver(body).status_code == 204
    assert _balance(org) == seconds
    entries = _grants(org)

    assert _deliver(body).status_code == 204
    assert _deliver(body).status_code == 204
    assert _balance(org) == seconds and _grants(org) == entries


def test_the_same_top_up_paid_event_under_a_new_event_id_still_grants_once(paddle):  # noqa: F811
    """Paddle can also send a second, distinct event for the same transaction; the purchase row guards that."""
    _c, org = _new_org()
    topup_id, txn, seconds = _purchase(org, 2500)

    def event():
        return _body({"event_id": f"evt_{uuid.uuid4().hex}", "event_type": "transaction.completed",
                      "occurred_at": "2026-10-02T00:00:00.000000Z", "data": _txn(org, topup_id, txn, 2500)})

    assert _deliver(event()).status_code == 204
    assert _deliver(event()).status_code == 204
    assert _balance(org) == seconds


def test_a_refund_adjustment_delivered_twice_is_applied_once(paddle):  # noqa: F811
    _c, org = _new_org()
    topup_id, txn, seconds = _purchase(org, 4000)
    paid = _body({"event_id": f"evt_{uuid.uuid4().hex}", "event_type": "transaction.completed",
                  "occurred_at": "2026-10-02T00:00:00.000000Z", "data": _txn(org, topup_id, txn, 4000)})
    assert _deliver(paid).status_code == 204
    refund = _body({"event_id": f"evt_{uuid.uuid4().hex}", "event_type": "adjustment.created",
                    "occurred_at": "2026-10-03T00:00:00.000000Z", "data": _adjustment(txn, subtotal=1000)})
    assert _deliver(refund).status_code == 204
    after_first = _balance(org)
    assert after_first == seconds - seconds * 1000 // 4000

    for _ in range(3):
        assert _deliver(refund).status_code == 204
    assert _balance(org) == after_first


# ---------------- signatures ----------------

def _assert_nothing_happened(org, sub_id, event_id, grants_before=0):
    assert _count(Subscription, external_subscription_id=sub_id) == 0
    assert _count(WebhookEvent, external_event_id=event_id) == 0
    assert _grants(org) == grants_before


def test_invalid_signatures_are_rejected_and_change_nothing(spies):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    event = _event("subscription.created", sub_id, org)
    body = _body(event)
    good = _sign(body)
    ts = good.split(";")[0]
    cases = {
        "missing header": None,
        "empty header": "",
        "garbage": "not-a-signature",
        "wrong secret": _sign(body, secret="a-different-secret"),
        "signature of a different body": _sign(body + b" "),
        "no h1": ts,
        "no ts": good.split(";")[1],
        "non-numeric ts": "ts=abc;h1=" + good.split("h1=")[1],
        "truncated digest": good[:-8],
    }
    for name, header in cases.items():
        resp = _post(None, signature=header, body=body)
        assert resp.status_code == 401, name
    assert _deliver(body + b" ", signature=good).status_code == 401  # tampered body, valid-looking header
    _assert_nothing_happened(org, sub_id, event["event_id"])
    assert spies == {"sync": 0, "after": 0}


def test_stale_and_future_timestamps_are_rejected_and_change_nothing(spies):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    event = _event("subscription.created", sub_id, org)
    body = _body(event)
    skew = get_settings().PADDLE_WEBHOOK_MAX_SKEW_SECONDS
    now = int(time.time())
    for label, ts in {"6 min old": now - skew - 60, "1 day old": now - 86400, "6 min in the future": now + skew + 60}.items():
        assert _deliver(body, signature=_sign(body, ts=ts)).status_code == 401, label  # correctly signed, but outside the window
    _assert_nothing_happened(org, sub_id, event["event_id"])
    assert spies == {"sync": 0, "after": 0}


def test_a_rejected_delivery_does_not_use_up_the_event_id():
    """A replayed/forged copy must not stop Paddle's genuine retry of the same event from being processed."""
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    event = _event("subscription.created", sub_id, org)
    body = _body(event)
    assert _deliver(body, signature=_sign(body, secret="attacker")).status_code == 401
    assert _deliver(body, signature=_sign(body, ts=int(time.time()) - 3600)).status_code == 401
    assert _count(Subscription, external_subscription_id=sub_id) == 0
    assert _deliver(body).status_code == 204
    assert _sub(sub_id).status == "active"


def test_a_signature_made_during_secret_rotation_is_accepted_when_any_h1_matches():
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    body = _body(_event("subscription.created", sub_id, org))
    good = _sign(body)
    ts, h1 = good.split(";")
    rotated = f"{ts};h1={'0' * 64};{h1}"  # the old secret's digest first, the new one second
    assert _deliver(body, signature=rotated).status_code == 204
    assert _sub(sub_id).status == "active"
