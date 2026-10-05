"""Paddle billing: webhook security/idempotency, the money gate, checkout."""
import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models.billing import Subscription
from app.db.models.platform import WebhookEvent
from app.main import app
from tests.test_tenant_isolation import _signup

SECRET = "pdl_ntfset_test_secret"
anon = TestClient(app)


@pytest.fixture(autouse=True)
def _paddle_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_API_KEY", "test_key")
    monkeypatch.setattr(s, "PADDLE_ENV", "sandbox")
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")


def _new_org():
    tag = uuid.uuid4().hex[:10]
    return _signup(f"bill-{tag}@billing-test.com", f"Billing Org {tag}")


def _event(event_type, sub_id, org_id, status="active", plan="starter", event_id=None, at="2026-10-01T00:00:00.000000Z"):
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": at,
        "notification_id": f"ntf_{uuid.uuid4().hex}",
        "data": {
            "id": sub_id,
            "status": status,
            "custom_data": {"organization_id": org_id, "plan_id": plan},
            "current_billing_period": {
                "starts_at": "2026-10-01T00:00:00.000000Z",
                "ends_at": "2026-11-01T00:00:00.000000Z",
            },
        },
    }


def _sign(body: bytes, secret=SECRET, ts=None) -> str:
    ts = str(int(time.time()) if ts is None else ts)
    h1 = hmac.new(secret.encode(), ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def _post(event: dict, signature: str | None = "auto", body: bytes | None = None):
    body = body if body is not None else json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["Paddle-Signature"] = _sign(body)
    elif signature is not None:
        headers["Paddle-Signature"] = signature
    return anon.post("/webhooks/paddle", content=body, headers=headers)


def _gate(db, org_id: str):
    from app.auth.deps import require_active_subscription

    return require_active_subscription(membership=SimpleNamespace(organization_id=uuid.UUID(org_id)), db=db)


def _sub(db, org_id: str):
    from app.services.billing_state import current_subscription

    db.rollback()
    return current_subscription(db, uuid.UUID(org_id))


# ---- webhook signature ----

def test_webhook_rejects_missing_signature():
    _c, org = _new_org()
    assert _post(_event("subscription.created", "sub_x1", org), signature=None).status_code == 401


def test_webhook_rejects_forged_signature():
    _c, org = _new_org()
    body = json.dumps(_event("subscription.created", "sub_x2", org)).encode()
    assert _post({}, signature=_sign(body, secret="wrong"), body=body).status_code == 401


def test_webhook_rejects_tampered_body():
    _c, org = _new_org()
    body = json.dumps(_event("subscription.created", "sub_x3", org)).encode()
    sig = _sign(body)
    assert _post({}, signature=sig, body=body + b" ").status_code == 401


def test_webhook_rejects_stale_timestamp():
    _c, org = _new_org()
    body = json.dumps(_event("subscription.created", "sub_x4", org)).encode()
    assert _post({}, signature=_sign(body, ts=int(time.time()) - 3600), body=body).status_code == 401


# ---- webhook behaviour ----

def test_subscription_created_then_canceled(db):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    assert _post(_event("subscription.created", sub_id, org, plan="growth")).status_code == 204
    sub = _sub(db, org)
    assert sub.status == "active" and sub.plan_id == "growth"
    assert sub.billing_provider == "paddle" and sub.external_subscription_id == sub_id
    assert sub.current_period_end is not None

    assert _post(_event("subscription.canceled", sub_id, org, status="canceled")).status_code == 204
    assert _sub(db, org).status == "canceled"


def test_duplicate_delivery_is_idempotent(db):
    _c, org = _new_org()
    evt = _event("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org)
    assert _post(evt).status_code == 204
    assert _post(evt).status_code == 204
    db.rollback()
    assert db.query(WebhookEvent).filter(WebhookEvent.external_event_id == evt["event_id"]).count() == 1
    assert db.query(Subscription).filter(Subscription.organization_id == uuid.UUID(org)).count() == 1


def test_processing_failure_is_retryable(monkeypatch, db):
    """Regression: a crash mid-processing must NOT leave a committed event row
    that makes Paddle's retry look like a duplicate."""
    import app.webhooks.paddle as pw

    _c, org = _new_org()
    evt = _event("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org)

    with monkeypatch.context() as m:
        m.setattr(pw, "_sync_subscription", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _post(evt).status_code == 500
    db.rollback()
    assert db.query(WebhookEvent).filter(WebhookEvent.external_event_id == evt["event_id"]).count() == 0

    assert _post(evt).status_code == 204
    assert _sub(db, org).status == "active"


def test_event_without_valid_org_is_ignored_not_crashing(db):
    evt = _event("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", str(uuid.uuid4()))
    assert _post(evt).status_code == 204


def test_stale_event_for_old_subscription_cannot_downgrade_current(db):
    _c, org = _new_org()
    old, new = f"sub_old{uuid.uuid4().hex[:8]}", f"sub_new{uuid.uuid4().hex[:8]}"
    assert _post(_event("subscription.created", new, org)).status_code == 204
    assert _post(_event("subscription.canceled", old, org, status="canceled")).status_code == 204
    sub = _sub(db, org)
    assert sub.status == "active" and sub.external_subscription_id == new


# ---- the money gate ----

def test_gate_blocks_org_without_subscription(db):
    _c, org = _new_org()
    with pytest.raises(HTTPException) as exc:
        _gate(db, org)
    assert exc.value.status_code == 402


def test_gate_passes_active_and_blocks_canceled_and_past_due(db):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_event("subscription.created", sub_id, org))
    assert _gate(db, org) is not None
    for bad in ("past_due", "canceled", "paused"):
        _post(_event("subscription.updated", sub_id, org, status=bad))
        db.rollback()
        with pytest.raises(HTTPException) as exc:
            _gate(db, org)
        assert exc.value.status_code == 402


def test_gate_is_tenant_scoped(db):
    _c_a, org_a = _new_org()
    _c_b, org_b = _new_org()
    _post(_event("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org_a))
    assert _gate(db, org_a) is not None
    with pytest.raises(HTTPException) as exc:
        _gate(db, org_b)
    assert exc.value.status_code == 402


def test_phone_number_purchase_blocked_without_subscription():
    c, org = _new_org()
    resp = c.post(f"/api/v1/orgs/{org}/phone-numbers", json={"country": "US", "area_code": "305"})
    assert resp.status_code == 402


# ---- checkout endpoint ----

class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_checkout_unauthenticated_rejected():
    _c, org = _new_org()
    assert anon.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"}).status_code == 401


def test_checkout_cross_tenant_404():
    c_a, _org_a = _new_org()
    _c_b, org_b = _new_org()
    assert c_a.post(f"/api/v1/orgs/{org_b}/billing/checkout-session", json={"plan": "starter"}).status_code == 404


def test_checkout_unknown_plan_400():
    c, org = _new_org()
    assert c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "platinum"}).status_code == 400


def test_checkout_missing_api_key_is_clean_502_not_500(monkeypatch):
    monkeypatch.setattr(get_settings(), "PADDLE_API_KEY", "")
    c, org = _new_org()
    resp = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"})
    assert resp.status_code == 502


def test_checkout_builds_transaction_server_side(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp

    c, org = _new_org()
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _FakeResp(201, {"data": {"id": "txn_123", "checkout": {"url": "http://localhost:3000/checkout?_ptxn=txn_123"}}})

    monkeypatch.setattr(pp.httpx, "post", fake_post)
    resp = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"})
    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "http://localhost:3000/checkout?_ptxn=txn_123"
    assert captured["url"] == "https://sandbox-api.paddle.com/transactions"
    assert captured["headers"]["Authorization"] == "Bearer test_key"
    assert captured["json"]["items"] == [{"price_id": "pri_starter", "quantity": 1}]
    assert captured["json"]["custom_data"] == {"organization_id": org, "plan_id": "starter"}


def test_checkout_paddle_error_is_clean_502(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp

    c, org = _new_org()
    monkeypatch.setattr(pp.httpx, "post", lambda *a, **k: _FakeResp(403, {"error": {}}))
    resp = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "growth"})
    assert resp.status_code == 502

def test_activate_blocked_without_subscription():
    c, org = _new_org()
    resp = c.post(
        f"/api/v1/orgs/{org}/activate",
        json={"agent_id": str(uuid.uuid4()), "phone_number_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 402

# ---------------- ordering / idempotency (Paddle is at-least-once and unordered) ----------------

def test_old_event_cannot_resurrect_a_canceled_subscription(db):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    t = lambda minute: f"2026-10-01T00:{minute:02d}:00.000000Z"
    assert _post(_event("subscription.created", sub_id, org, at=t(1))).status_code == 204
    assert _post(_event("subscription.canceled", sub_id, org, status="canceled", at=t(5))).status_code == 204
    # an older "active" update is delivered late
    assert _post(_event("subscription.updated", sub_id, org, status="active", at=t(3))).status_code == 204

    sub = _sub(db, org)
    assert sub.status == "canceled"
    from app.services.billing_state import has_active_subscription

    assert has_active_subscription(db, uuid.UUID(org)) is False
    with pytest.raises(HTTPException):
        _gate(db, org)


def test_newer_event_still_applies_after_a_stale_one_was_ignored(db):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    t = lambda minute: f"2026-10-01T00:{minute:02d}:00.000000Z"
    _post(_event("subscription.created", sub_id, org, status="past_due", at=t(10)))
    _post(_event("subscription.updated", sub_id, org, status="active", at=t(2)))  # stale
    assert _sub(db, org).status == "past_due"
    _post(_event("subscription.updated", sub_id, org, status="active", at=t(20)))  # newer
    assert _sub(db, org).status == "active"


def test_duplicate_delivery_changes_state_once(db):
    _c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    evt = _event("subscription.created", sub_id, org, at="2026-10-01T00:10:00.000000Z")
    for _ in range(3):
        assert _post(evt).status_code == 204
    db.rollback()
    assert db.query(WebhookEvent).filter(WebhookEvent.external_event_id == evt["event_id"]).count() == 1
    assert db.query(Subscription).filter(Subscription.organization_id == uuid.UUID(org)).count() == 1


def test_event_without_occurred_at_is_rejected():
    _c, org = _new_org()
    evt = _event("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org)
    del evt["occurred_at"]
    assert _post(evt).status_code == 400


def test_a_late_event_for_an_old_subscription_cannot_deactivate_the_current_one(db):
    _c, org = _new_org()
    old, new = f"sub_old{uuid.uuid4().hex[:8]}", f"sub_new{uuid.uuid4().hex[:8]}"
    _post(_event("subscription.created", new, org, at="2026-10-02T00:00:00.000000Z"))
    _post(_event("subscription.canceled", old, org, status="canceled", at="2026-10-01T00:00:00.000000Z"))
    from app.services.billing_state import has_active_subscription

    assert has_active_subscription(db, uuid.UUID(org)) is True
    assert _sub(db, org).external_subscription_id == new


# ---------------- second checkout ----------------

def _count_paddle_posts(monkeypatch):
    import app.providers.billing.paddle_billing_provider as pp

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _FakeResp(201, {"data": {"id": "txn_1", "checkout": {"url": "http://localhost:3000/checkout?_ptxn=txn_1"}}})

    monkeypatch.setattr(pp.httpx, "post", fake_post)
    return calls


@pytest.mark.parametrize("state", ["active", "trialing", "past_due", "paused"])
def test_second_checkout_is_refused_and_creates_no_paddle_transaction(monkeypatch, state):
    calls = _count_paddle_posts(monkeypatch)
    c, org = _new_org()
    _post(_event("subscription.created", f"sub_{uuid.uuid4().hex[:12]}", org, status=state))
    resp = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "growth"})
    assert resp.status_code == 409
    assert calls == []


def test_checkout_allowed_again_after_cancellation(monkeypatch):
    calls = _count_paddle_posts(monkeypatch)
    c, org = _new_org()
    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    _post(_event("subscription.created", sub_id, org, at="2026-10-01T00:01:00.000000Z"))
    _post(_event("subscription.canceled", sub_id, org, status="canceled", at="2026-10-01T00:02:00.000000Z"))
    assert c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"}).status_code == 200
    assert len(calls) == 1


def test_double_click_checkout_creates_one_transaction(monkeypatch):
    from app.core.locks import redis_lock

    calls = _count_paddle_posts(monkeypatch)
    c, org = _new_org()
    with redis_lock(f"checkout:{org}", ttl_seconds=30):  # a first request is mid-flight
        resp = c.post(f"/api/v1/orgs/{org}/billing/checkout-session", json={"plan": "starter"})
    assert resp.status_code == 409 and calls == []
