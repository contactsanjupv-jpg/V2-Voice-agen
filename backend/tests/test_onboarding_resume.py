"""Onboarding state comes from the database, so refresh / return / another device resumes correctly."""
import uuid

from app.db.base import SessionLocal
from app.db.models.business import Business
from app.db.models.calls import Call, CallDirection
from app.db.models.voice_agent import Agent
from tests.test_agent_config import FakeRetell, _agent_id, _body, _put  # noqa: F401
from tests.test_tenant_isolation import _signup
import pytest

from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.providers.call.retell_call_provider import RetellCallProvider
from app.db.models.voice_agent import Voice


@pytest.fixture
def fake(monkeypatch):
    f = FakeRetell()
    monkeypatch.setattr("app.services.agent_service.RetellAgentProvider", lambda: RetellAgentProvider(client=f))
    monkeypatch.setattr("app.api.v1.activation.RetellCallProvider", lambda: RetellCallProvider(client=f))
    return f


def _org():
    tag = uuid.uuid4().hex[:10]
    return _signup(f"onb-{tag}@onb-test.com", f"Onb Org {tag}")


def _state(c, org):
    return c.get(f"/api/v1/orgs/{org}/onboarding").json()


def _voice():
    db = SessionLocal()
    v = Voice(retell_voice_id=f"voice_{uuid.uuid4().hex[:8]}", name="V", provider="elevenlabs")
    db.add(v)
    db.commit()
    vid = str(v.id)
    db.close()
    return vid


APPROVE = {"name": "Maple Dental", "industry": "Dental", "address": "1 Main St", "description": "d", "hours": None,
           "phone": "+15550000000", "services": ["Cleaning"], "faqs": [{"question": "Q?", "answer": "A."}], "policies": []}


def test_new_org_starts_at_the_website_step():
    c, org = _org()
    assert _state(c, org)["step"] == "website"


def test_refresh_during_review_returns_the_imported_data_without_reimporting():
    c, org = _org()
    extracted = {"business_name": "Maple Dental", "description": "Family dentist", "industry": "Dental", "services": ["Cleaning"],
                 "address": "1 Main St", "phone": None, "hours": None, "faqs": [], "policies": []}
    db = SessionLocal()
    db.add(Business(organization_id=uuid.UUID(org), name="Maple Dental", website_url="https://maple.example",
                    raw_import_snapshot={"pages": ["https://maple.example"], "extracted": extracted}))
    db.commit()
    db.close()
    for _ in range(3):  # "refresh" three times
        s = _state(c, org)
        assert s["step"] == "review" and s["structured_info"] == extracted and s["business_id"]


def test_manual_business_resumes_at_review():
    c, org = _org()
    biz = c.post(f"/api/v1/orgs/{org}/businesses/create-manual", json={"name": "Corner Salon"}).json()
    s = _state(c, org)
    assert s["step"] == "review" and s["business_id"] == biz["id"] and s["structured_info"]["business_name"] == "Corner Salon"


def test_full_progression_voice_behavior_test_done(fake):
    c, org = _org()
    biz = c.post(f"/api/v1/orgs/{org}/businesses/create-manual", json={"name": "Maple Dental"}).json()
    assert c.post(f"/api/v1/orgs/{org}/businesses/{biz['id']}/approve", json=APPROVE).status_code == 200
    assert _state(c, org)["step"] == "voice"

    voice = _voice()
    fake.fail["/create-retell-llm"] = 1  # the save reaches us but Retell is down
    assert _put(c, org, biz["id"], _body(voice)).status_code == 502
    s = _state(c, org)
    assert s["step"] == "behavior" and s["agent"]["synced"] is False and s["agent"]["voice_id"] == voice

    assert _put(c, org, biz["id"], _body(voice)).status_code == 200
    s = _state(c, org)
    assert s["step"] == "test" and s["agent"]["synced"] is True and s["tested"] is False

    agent_id = _agent_id(c, org)
    db = SessionLocal()
    db.add(Call(organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:8]}", agent_id=uuid.UUID(agent_id),
                direction=CallDirection.test, status="ended", duration_seconds=2))
    db.commit()
    db.close()
    assert _state(c, org)["step"] == "test"  # a 2-second call is not a real test

    db = SessionLocal()
    db.add(Call(organization_id=uuid.UUID(org), retell_call_id=f"call_{uuid.uuid4().hex[:8]}", agent_id=uuid.UUID(agent_id),
                direction=CallDirection.test, status="ended", duration_seconds=40))
    db.commit()
    db.close()
    s = _state(c, org)
    assert s["step"] == "done" and s["tested"] is True


def test_onboarding_state_is_tenant_scoped():
    c1, org1 = _org()
    c2, org2 = _org()
    c1.post(f"/api/v1/orgs/{org1}/businesses/create-manual", json={"name": "Only Mine"})
    assert _state(c2, org2)["step"] == "website"
    assert c2.get(f"/api/v1/orgs/{org1}/onboarding").status_code == 404
