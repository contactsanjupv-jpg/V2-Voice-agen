"""
Route-by-route tenant isolation, driven by the app's REAL route table (so a route added later is covered automatically).

  1. Every organization-scoped route sits behind `current_membership`; every other route is on an explicit allow-list.
  2. Tenant B calling EVERY organization route against tenant A's organization id is refused (404, never a 2xx).
  3. Tenant B calling every by-id route through B's OWN organization with tenant A's real ids gets nothing of A's,
     and A's data is unchanged afterwards.
"""
import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth.deps import current_membership
from app.db.base import SessionLocal
from app.db.models.business import Business
from app.db.models.calls import Call, CallDirection, CallTranscript
from app.db.models.crm import Lead, LeadStatus
from app.db.models.telephony import PhoneNumber
from app.db.models.voice_agent import Agent, AgentStatus, Voice
from app.main import app
from app.config import get_settings
from tests.billing_helpers import subscribe
from tests.test_tenant_isolation import _signup

ORG_PREFIX = "/api/v1/orgs/{organization_id}"
SECRET = "SECRET-TENANT-A-0451"

# Routes that are deliberately not organization-scoped, and why.
ALLOWED_NON_ORG_ROUTES = {
    ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/logout"), ("POST", "/api/v1/auth/signup"),
    ("POST", "/api/v1/auth/password-reset/request"), ("POST", "/api/v1/auth/password-reset/confirm"),
    ("GET", "/api/v1/auth/me"), ("POST", "/api/v1/auth/password/change"),  # the caller's own user
    ("GET", "/api/v1/orgs/me"),  # lists only the caller's own memberships
    ("GET", "/api/v1/plans"), ("GET", "/api/v1/public-config"),  # public catalog
    ("GET", "/api/v1/voices"),  # shared voice catalog, not tenant data
    ("POST", "/api/v1/admin/refunds"),  # platform-admin only (require_platform_admin)
    ("GET", "/health"), ("GET", "/health/ready"), ("GET", "/health/alerts"),  # health; /alerts needs a bearer token
    ("POST", "/webhooks/paddle"), ("POST", "/webhooks/retell"),  # signature-verified provider webhooks
}


def _walk(routes):
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _walk(nested.routes)
        else:
            yield route


def _api_routes() -> list[tuple[str, APIRoute]]:
    out = []
    for route in _walk(app.routes):
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, route))
    return out


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def test_every_organization_route_requires_membership_and_every_other_route_is_allow_listed():
    unguarded, unlisted = [], []
    for method, route in _api_routes():
        if route.path.startswith(ORG_PREFIX):
            if not _depends_on(route.dependant, current_membership):
                unguarded.append((method, route.path))
        elif (method, route.path) not in ALLOWED_NON_ORG_ROUTES:
            unlisted.append((method, route.path))
    assert not unguarded, f"organization routes without current_membership: {unguarded}"
    assert not unlisted, f"routes outside an organization that are not on the allow-list (review them): {unlisted}"


@pytest.fixture(scope="module")
def tenants():
    tag = uuid.uuid4().hex[:8]
    client_a, org_a = _signup(f"routes-a-{tag}@iso-routes.com", f"Routes A {tag}")
    client_b, org_b = _signup(f"routes-b-{tag}@iso-routes.com", f"Routes B {tag}")
    db = SessionLocal()
    try:
        biz = Business(organization_id=uuid.UUID(org_a), name=f"{SECRET} Biz", website_url="https://a.example")
        voice = Voice(retell_voice_id=f"voice_{uuid.uuid4().hex[:8]}", name="V", provider="elevenlabs")
        db.add_all([biz, voice])
        db.flush()
        agent = Agent(
            organization_id=uuid.UUID(org_a), business_id=biz.id, voice_id=voice.id, name=f"{SECRET} Agent",
            personality="friendly", language="en-US", tasks={}, status=AgentStatus.active, version=1, synced_version=1,
            retell_agent_id=f"agent_{uuid.uuid4().hex[:10]}", retell_llm_id=f"llm_{uuid.uuid4().hex[:10]}",
        )
        db.add(agent)
        db.flush()
        number = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
        phone = PhoneNumber(organization_id=uuid.UUID(org_a), agent_id=agent.id, retell_phone_number_id=number, number=number)
        call = Call(organization_id=uuid.UUID(org_a), retell_call_id=f"call_{uuid.uuid4().hex[:12]}", direction=CallDirection.inbound,
                    status="ended", caller_number="+15550001111", summary=f"{SECRET} summary")
        db.add_all([phone, call])
        db.flush()
        db.add(CallTranscript(call_id=call.id, transcript={"text": f"{SECRET} transcript", "segments": []}))
        lead = Lead(organization_id=uuid.UUID(org_a), call_id=call.id, name=f"{SECRET} Lead", phone="+15551112222", status=LeadStatus.new)
        db.add(lead)
        db.commit()
        ids = {
            "organization_id": org_a, "business_id": str(biz.id), "agent_id": str(agent.id), "call_id": str(call.id),
            "lead_id": str(lead.id), "phone_number_id": str(phone.id),
        }
    finally:
        db.close()
    # Tenant B is a paying Growth customer: plan gates (402) must not be what stops it, only ownership.
    with pytest.MonkeyPatch.context() as mp:
        settings = get_settings()
        mp.setattr(settings, "PADDLE_STARTER_PRICE_ID", "pri_starter")
        mp.setattr(settings, "PADDLE_GROWTH_PRICE_ID", "pri_growth")
        subscribe(org_a, "growth")
        subscribe(org_b, "growth")
        yield {"a": client_a, "b": client_b, "org_a": org_a, "org_b": org_b, "ids_a": ids}


def _fill(path: str, **values) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), str(uuid.uuid4())), path)


def _call(client: TestClient, method: str, url: str):
    kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
    return client.request(method, url, **kwargs)


def _org_routes():
    return [(m, r) for m, r in _api_routes() if r.path.startswith(ORG_PREFIX)]


def test_tenant_b_is_refused_on_every_organization_route_of_tenant_a(tenants):
    failures = []
    for method, route in _org_routes():
        url = _fill(route.path, **tenants["ids_a"])
        resp = _call(tenants["b"], method, url)
        if resp.status_code != 404 or SECRET in resp.text:
            failures.append((method, route.path, resp.status_code))
    assert not failures, failures


def test_unauthenticated_requests_are_refused_on_every_organization_route(tenants):
    anonymous = TestClient(app)
    failures = []
    for method, route in _org_routes():
        resp = _call(anonymous, method, _fill(route.path, **tenants["ids_a"]))
        if resp.status_code != 401:
            failures.append((method, route.path, resp.status_code))
    assert not failures, failures


def test_tenant_b_cannot_reach_tenant_a_records_by_id_through_its_own_organization(tenants):
    ids = dict(tenants["ids_a"], organization_id=tenants["org_b"])
    by_id = [(m, r) for m, r in _org_routes() if re.search(r"\{(agent|business|call|lead|phone_number)_id\}", r.path)]
    assert len(by_id) >= 10  # the guard itself must be looking at the real by-id routes
    failures = []
    for method, route in by_id:
        resp = _call(tenants["b"], method, _fill(route.path, **ids))
        # 404 = not found for this tenant; 402/403/409/422 = refused by a gate or validation. Never a success, never A's data.
        if resp.status_code in range(200, 300) or resp.status_code >= 500 or SECRET in resp.text:
            failures.append((method, route.path, resp.status_code))
    assert not failures, failures


def test_tenant_b_by_id_attempts_leave_tenant_a_data_unchanged(tenants):
    ids = dict(tenants["ids_a"], organization_id=tenants["org_b"])
    b = tenants["b"]
    assert b.patch(_fill(ORG_PREFIX + "/leads/{lead_id}", **ids), json={"status": "contacted"}).status_code == 404
    assert b.delete(_fill(ORG_PREFIX + "/phone-numbers/{phone_number_id}", **ids)).status_code == 404
    assert b.get(_fill(ORG_PREFIX + "/calls/{call_id}", **ids)).status_code == 404
    assert b.get(_fill(ORG_PREFIX + "/calls/{call_id}/transcript", **ids)).status_code == 404  # B has the plan: ownership decides
    search = b.get(_fill(ORG_PREFIX + "/calls/search", **ids), params={"q": "SECRET-TENANT"})
    assert search.status_code == 200 and search.json() == []
    assert b.get(_fill(ORG_PREFIX + "/businesses/{business_id}", **ids)).status_code == 404
    db = SessionLocal()
    try:
        assert db.get(Lead, uuid.UUID(ids["lead_id"])).status == LeadStatus.new
        assert db.get(PhoneNumber, uuid.UUID(ids["phone_number_id"])) is not None
    finally:
        db.close()
    # and A can still read its own records
    a = tenants["a"]
    own = dict(ids, organization_id=tenants["org_a"])
    assert a.get(_fill(ORG_PREFIX + "/calls/{call_id}", **own)).status_code == 200
    assert SECRET in a.get(_fill(ORG_PREFIX + "/calls/{call_id}/transcript", **own)).text
    assert a.get(_fill(ORG_PREFIX + "/businesses/{business_id}", **own)).status_code == 200
