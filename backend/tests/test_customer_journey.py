"""
One customer, start to finish, through the REAL routes and workers (Postgres, signed webhooks over the HTTP layer,
the entitlement worker with a fake phone provider). Database state is asserted after every step.

Not simulated by editing rows: sign-up, login, every subscription change (signed Paddle webhooks), every call
(signed Retell webhooks), usage/credit summaries, dashboard billing status, enforcement, recovery, cancellation.
Seeded because it needs a live Retell account: the receptionist and its phone number (tests.test_call_pipeline._seed).
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.billing import Subscription
from app.db.models.calls import Call
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.main import app
from app.services.entitlement import enforce_entitlements
from tests.fakes import FakePhone
from tests.test_call_pipeline import KEY, _call, _event_row, _post, _seed
from tests.test_tenant_isolation import _signup

SECRET = "whsec-journey-test"
anon = TestClient(app)


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")
    monkeypatch.setattr(s, "RETELL_API_KEY", KEY)
    monkeypatch.setattr(s, "PAST_DUE_GRACE_DAYS", 3)
    monkeypatch.setattr(s, "OVERAGE_GRACE_MINUTES", 30)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _paddle(event_type, sub_id, org, status, occurred_at, period=(None, None)):
    start, end = period
    data = {
        "id": sub_id, "status": status, "customer_id": "ctm_journey",
        "items": [{"price": {"id": "pri_starter"}, "quantity": 1}],
        "custom_data": {"organization_id": org},
        "current_billing_period": {"starts_at": _iso(start), "ends_at": _iso(end)} if start else None,
    }
    body = json.dumps({"event_id": f"evt_{uuid.uuid4().hex[:12]}", "event_type": event_type,
                       "occurred_at": _iso(occurred_at), "data": data}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return anon.post("/webhooks/paddle", content=body, headers={"Paddle-Signature": f"ts={ts};h1={sig}", "Content-Type": "application/json"})


def _sub_row(sub_id):
    db = SessionLocal()
    try:
        row = db.query(Subscription).filter_by(external_subscription_id=sub_id).one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _agent_and_number(org):
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one()
        phone = db.query(PhoneNumber).filter_by(organization_id=uuid.UUID(org)).one()
        return agent.status, phone.status
    finally:
        db.close()


def _enforce(org, at):
    db = SessionLocal()
    try:
        return enforce_entitlements(db, FakePhone(), now=at, organization_ids={uuid.UUID(org)})
    finally:
        db.close()


def test_a_customer_from_signup_to_cancellation():
    tag = uuid.uuid4().hex[:8]
    client, org = _signup(f"journey-{tag}@journey-test.com", f"Journey {tag}")
    base = f"/api/v1/orgs/{org}"
    now = datetime.now(timezone.utc)

    # 1-2. registered and logged in: the session works, and there is no plan yet (so no paid access)
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get(f"{base}/billing/subscription").json() is None
    assert client.get(f"{base}/usage/credits").json()["entitled"] is False

    # 3-5. receptionist + number exist (seeded: needs Retell); the front-end cannot create a subscription
    agent_id, number = _seed(org)
    assert _agent_and_number(org) == (AgentStatus.active, PhoneNumberStatus.active)
    forged = client.post(f"{base}/billing/subscription", json={"status": "active", "plan_id": "growth"})
    assert forged.status_code in (404, 405) and client.get(f"{base}/billing/subscription").json() is None

    # 6. paid: ONLY a signed Paddle webhook creates the subscription; it grants the plan's credits
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    start, end = now - timedelta(days=1), now + timedelta(days=29)
    assert _paddle("subscription.created", sub_id, org, "active", now - timedelta(hours=2), (start, end)).status_code == 204
    sub = client.get(f"{base}/billing/subscription").json()
    assert sub["status"] == "active" and sub["entitled"] is True and sub["plan_id"] == "starter"
    credits = client.get(f"{base}/usage/credits").json()
    assert credits["entitled"] is True and credits["included_granted_seconds"] > 0 and credits["included_used_seconds"] == 0

    # 7-9. a real inbound call arrives (signed Retell webhook): recorded, and usage is charged to the right org
    cid = f"call_{uuid.uuid4().hex[:10]}"
    ended_ms = int(time.time() * 1000) - 5_000
    call = _call(cid, agent_id, number, start_timestamp=ended_ms - 61_499, end_timestamp=ended_ms)  # a call that just ended
    assert _post("call_ended", call).status_code == 204
    assert _event_row(cid, "call_ended").status == "processed"
    credits = client.get(f"{base}/usage/credits").json()
    assert credits["included_used_seconds"] > 0 and credits["calls_handled"] == 1
    assert _post("call_ended", call).status_code == 204  # duplicate delivery
    assert client.get(f"{base}/usage/credits").json()["calls_handled"] == 1  # not billed twice
    db = SessionLocal()
    try:
        assert db.query(Call).filter_by(retell_call_id=cid).count() == 1
    finally:
        db.close()

    # 10-12. payment fails -> past_due: the dashboard says so, with the real grace deadline; service continues in grace
    failed_at = now - timedelta(hours=1)
    assert _paddle("subscription.past_due", sub_id, org, "past_due", failed_at, (start, end)).status_code == 204
    assert _sub_row(sub_id).status == "past_due"
    view = client.get(f"{base}/billing/subscription").json()
    assert view["status"] == "past_due" and view["entitled"] is False and view["service_ends_at"] is not None
    _enforce(org, now)
    assert _agent_and_number(org)[0] == AgentStatus.active  # inside the grace, nothing used beyond the limit

    # 11 (continued). past the grace period the worker pauses the receptionist (and the dashboard agrees)
    _enforce(org, now + timedelta(days=4))
    assert _agent_and_number(org)[0] == AgentStatus.paused
    assert client.get(f"{base}/billing/subscription").json()["status"] == "past_due"

    # 13-15. payment recovers (a LATER signed event): active again, entitled, and an OLDER stale event can't undo it
    recovered_at = now + timedelta(days=5)
    assert _paddle("subscription.updated", sub_id, org, "active", recovered_at, (start, end + timedelta(days=30))).status_code == 204
    assert _sub_row(sub_id).status == "active"
    view = client.get(f"{base}/billing/subscription").json()
    assert view["status"] == "active" and view["entitled"] is True
    assert _paddle("subscription.past_due", sub_id, org, "past_due", failed_at, (start, end)).status_code == 204  # replay of old news
    assert _sub_row(sub_id).status == "active"

    # 16-17. the customer cancels: entitlement ends and stays ended, even if a stale 'active' arrives afterwards
    canceled_at = now + timedelta(days=6)
    assert _paddle("subscription.canceled", sub_id, org, "canceled", canceled_at, (start, end)).status_code == 204
    view = client.get(f"{base}/billing/subscription").json()
    assert view["status"] == "canceled" and view["entitled"] is False
    assert client.get(f"{base}/usage/credits").json()["entitled"] is False
    assert _paddle("subscription.activated", sub_id, org, "active", now + timedelta(days=2), (start, end)).status_code == 204
    assert _sub_row(sub_id).status == "canceled"
    _enforce(org, now + timedelta(days=6, hours=1))
    assert _agent_and_number(org)[0] == AgentStatus.paused
