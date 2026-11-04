"""
The receptionist-creation and phone-provisioning workflow, driven through the REAL routes and services - nothing is seeded
into the database. Only the two outbound Retell edges are replaced: the Retell HTTP client behind the agent provider
(FakeRetellClient) and the phone-number provider (tests.fakes.FakePhone). Everything between them - request validation,
tenant and plan gates, locks, persistence, idempotency, retry reuse, activation verification, call attribution and
billing - is the application's own code.

What this does NOT prove: that Retell accepts these payloads or behaves like the fakes. That needs a Retell account
(docs/RETELL_VERIFICATION.md); it is still UNVERIFIED.
"""
import json
import time
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.calls import Call
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus, Voice
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.providers.retell_client import RetellAPIError
from tests.fakes import FakePhone
from tests.billing_helpers import live_period
from tests.test_billing import _event, _post as _paddle_post
from tests.test_call_pipeline import KEY, _call, _event_row, _post as _retell_post
from tests.test_tenant_isolation import _signup


class FakeRetellClient:
    """Records every request the agent provider makes; answers like Retell's create endpoints."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_on: dict[str, int] = {}  # path -> number of upcoming failures

    def request(self, method, path, json=None, **_):
        self.requests.append((method, path, json))
        if self.fail_on.get(path, 0) > 0:
            self.fail_on[path] -= 1
            raise RetellAPIError(500, f"injected failure on {path}")
        if path == "/create-retell-llm":
            return {"llm_id": f"llm_{uuid.uuid4().hex[:10]}"}
        if path == "/create-agent":
            return {"agent_id": f"agent_{uuid.uuid4().hex[:10]}"}
        return {}

    def paths(self, method=None):
        return [p for m, p, _ in self.requests if method in (None, m)]


@pytest.fixture
def world(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "PADDLE_WEBHOOK_SECRET", "pdl_ntfset_test_secret")
    monkeypatch.setattr(s, "PADDLE_STARTER_PRICE_ID", "pri_starter")
    monkeypatch.setattr(s, "PADDLE_GROWTH_PRICE_ID", "pri_growth")
    monkeypatch.setattr(s, "RETELL_API_KEY", KEY)
    retell, phones = FakeRetellClient(), FakePhone()
    monkeypatch.setattr("app.services.agent_service.RetellAgentProvider", lambda: RetellAgentProvider(client=retell))
    monkeypatch.setattr("app.services.phone_provisioning.RetellPhoneProvider", lambda: phones)
    monkeypatch.setattr("app.services.activation.RetellPhoneProvider", lambda: phones)
    return retell, phones


def _paying_customer():
    tag = uuid.uuid4().hex[:8]
    client, org = _signup(f"prov-{tag}@prov-test.com", f"Prov {tag}")
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    assert _paddle_post(_event("subscription.created", sub_id, org, at="2026-10-01T00:00:00.000000Z", period=live_period())).status_code == 204
    return client, org


def _voice_id():
    """The voice catalog is filled from Retell by a worker (unverifiable here); one catalog row, read back through the API."""
    db = SessionLocal()
    try:
        db.add(Voice(retell_voice_id=f"voice_{uuid.uuid4().hex[:8]}", name="Test Voice", provider="elevenlabs"))
        db.commit()
    finally:
        db.close()
    return None


def _first_voice(client):
    _voice_id()
    voices = client.get("/api/v1/voices").json()
    assert voices, "the voice catalog must be readable through the API"
    return voices[0]["id"]


def _agent_body(voice_id, name="Ava"):
    return {"name": name, "personality": "friendly", "language": "en-US", "voice_id": voice_id,
            "tasks": {"answer_questions": True, "capture_leads": True}}


def _create_business(client, org, name):
    resp = client.post(f"/api/v1/orgs/{org}/businesses/create-manual", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_new_customer_creates_a_receptionist_gets_a_number_and_goes_live(world):
    retell, phones = world
    client, org = _paying_customer()
    base = f"/api/v1/orgs/{org}"
    name = f"Smith Plumbing {uuid.uuid4().hex[:5]}"

    # no receptionist, no number yet
    assert client.get(f"{base}/agents").json() == [] and client.get(f"{base}/phone-numbers").json() == []

    # 1. the receptionist is created through the real route, which pushes the LLM and the agent to Retell once each
    business_id = _create_business(client, org, name)
    resp = client.put(f"{base}/agents/{business_id}", json=_agent_body(_first_voice(client)))
    assert resp.status_code == 200
    agent = resp.json()
    assert agent["synced"] is True and agent["retell_agent_id"].startswith("agent_") and agent["status"] != "active"
    assert retell.paths("POST") == ["/create-retell-llm", "/create-agent"]
    llm_payload = retell.requests[0][2]
    assert name in json.dumps(llm_payload)  # the prompt carries this customer's business
    assert json.dumps(retell.requests[1][2]).count("/webhooks/retell") == 1  # events go back to OUR webhook

    # saving again updates the same Retell resources - it never creates duplicates
    assert client.put(f"{base}/agents/{business_id}", json=_agent_body(_first_voice(client), "Ava 2")).status_code == 200
    assert retell.paths("POST") == ["/create-retell-llm", "/create-agent"]
    assert any(p.startswith("/update-agent/") for p in retell.paths("PATCH"))
    db = SessionLocal()
    try:
        assert db.query(Agent).filter_by(organization_id=uuid.UUID(org)).count() == 1
    finally:
        db.close()

    # 2. the number is provisioned through the real route; repeating the request returns the same single number
    first = client.post(f"{base}/phone-numbers", json={"country": "US", "area_code": "415"})
    assert first.status_code == 201
    again = client.post(f"{base}/phone-numbers", json={"country": "US", "area_code": "415"})
    assert again.status_code in (200, 201) and again.json()["id"] == first.json()["id"]
    assert phones.purchases == 1
    number = first.json()["number"]
    assert [n["id"] for n in client.get(f"{base}/phone-numbers").json()] == [first.json()["id"]]

    # 3. going live is verified against the provider: the number is routed to THIS agent, then the agent turns on
    live = client.post(f"{base}/activate", json={"agent_id": agent["id"], "phone_number_id": first.json()["id"]})
    assert live.status_code == 200 and live.json()["status"] == "active"
    assert (first.json()["id"] and phones.assigns[-1][1] == agent["retell_agent_id"])
    db = SessionLocal()
    try:
        row = db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one()
        phone = db.query(PhoneNumber).filter_by(organization_id=uuid.UUID(org)).one()
        assert row.status == AgentStatus.active and phone.status == PhoneNumberStatus.active and phone.agent_id == row.id
    finally:
        db.close()

    # 4. an inbound call to that number (signed Retell webhook, ids from the workflow above) is recorded and billed here
    cid = f"call_{uuid.uuid4().hex[:10]}"
    ended = int(time.time() * 1000) - 5_000
    call = _call(cid, agent["retell_agent_id"], number, start_timestamp=ended - 45_000, end_timestamp=ended, duration_ms=45_000)
    assert _retell_post("call_ended", call).status_code == 204
    assert _event_row(cid, "call_ended").status == "processed"
    db = SessionLocal()
    try:
        assert str(db.query(Call).filter_by(retell_call_id=cid).one().organization_id) == org
    finally:
        db.close()
    assert client.get(f"{base}/usage/credits").json()["calls_handled"] == 1


def test_a_retell_failure_while_creating_the_agent_is_reported_and_the_retry_reuses_the_llm(world):
    retell, _phones = world
    client, org = _paying_customer()
    base = f"/api/v1/orgs/{org}"
    business_id = _create_business(client, org, "Retry Plumbing")
    voice = _first_voice(client)
    retell.fail_on["/create-agent"] = 1

    failed = client.put(f"{base}/agents/{business_id}", json=_agent_body(voice))
    assert failed.status_code == 502  # saved, but not applied - never a silent success
    db = SessionLocal()
    try:
        saved = db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one()
        assert saved.retell_agent_id is None and saved.retell_llm_id is not None  # the LLM was kept for the retry
        llm_id = saved.retell_llm_id
    finally:
        db.close()
    assert client.get(f"{base}/agents").json()[0]["synced"] is False

    ok = client.put(f"{base}/agents/{business_id}", json=_agent_body(voice))
    assert ok.status_code == 200 and ok.json()["synced"] is True
    assert retell.paths("POST").count("/create-retell-llm") == 1  # no orphaned second LLM
    assert any(p == f"/update-retell-llm/{llm_id}" for p in retell.paths("PATCH"))


def test_a_number_the_provider_cannot_confirm_does_not_take_the_receptionist_live(world):
    retell, phones = world
    client, org = _paying_customer()
    base = f"/api/v1/orgs/{org}"
    business_id = _create_business(client, org, "Unverified Plumbing")
    agent = client.put(f"{base}/agents/{business_id}", json=_agent_body(_first_voice(client))).json()
    number = client.post(f"{base}/phone-numbers", json={"country": "US", "area_code": "415"}).json()
    phones.assign_is_noop = True  # the provider accepts the assignment but does not actually route the number

    resp = client.post(f"{base}/activate", json={"agent_id": agent["id"], "phone_number_id": number["id"]})
    assert resp.status_code == 502
    db = SessionLocal()
    try:
        assert db.query(Agent).filter_by(organization_id=uuid.UUID(org)).one().status != AgentStatus.active
    finally:
        db.close()


def test_provisioning_is_refused_without_a_paid_plan_and_nothing_is_bought(world):
    _retell, phones = world
    tag = uuid.uuid4().hex[:8]
    client, org = _signup(f"noplan-{tag}@prov-test.com", f"NoPlan {tag}")
    resp = client.post(f"/api/v1/orgs/{org}/phone-numbers", json={"country": "US", "area_code": "415"})
    assert resp.status_code in (402, 403)
    assert phones.purchases == 0


def test_another_customer_cannot_see_or_activate_this_customers_receptionist_or_number(world):
    retell, phones = world
    client_a, org_a = _paying_customer()
    client_b, org_b = _paying_customer()
    base_a = f"/api/v1/orgs/{org_a}"
    business_id = _create_business(client_a, org_a, "A Plumbing")
    agent = client_a.put(f"{base_a}/agents/{business_id}", json=_agent_body(_first_voice(client_a))).json()
    number = client_a.post(f"{base_a}/phone-numbers", json={"country": "US", "area_code": "415"}).json()

    assert client_b.get(f"{base_a}/agents").status_code == 404
    assert client_b.get(f"{base_a}/phone-numbers").status_code == 404
    # B, inside B's own organization, naming A's ids
    base_b = f"/api/v1/orgs/{org_b}"
    stolen = client_b.post(f"{base_b}/activate", json={"agent_id": agent["id"], "phone_number_id": number["id"]})
    assert stolen.status_code == 404
    assert client_b.delete(f"{base_b}/phone-numbers/{number['id']}").status_code == 404
    assert client_b.get(f"{base_b}/agents").json() == [] and phones.assigns == []
