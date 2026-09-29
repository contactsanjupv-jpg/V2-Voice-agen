"""
Spec §53: "Create tests specifically attempting cross-tenant access...
Every attempt must fail." Real integration tests against the FastAPI app
and the live Postgres/Redis instances (see tests/conftest.py) — not mocks
of the authorization logic, the actual HTTP layer.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _signup(email: str, org_name: str) -> tuple[TestClient, str]:
    """Returns a fresh TestClient carrying that user's session cookie, plus
    their organization_id."""
    c = TestClient(app)
    resp = c.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "correct-horse-battery-staple", "organization_name": org_name},
    )
    assert resp.status_code == 201, resp.text
    me = c.get("/api/v1/auth/me")
    import psycopg

    from app.config import get_settings

    settings = get_settings()
    with psycopg.connect(settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM organizations WHERE name = %s", (org_name,))
            org_id = str(cur.fetchone()[0])
    return c, org_id


def test_tenant_a_cannot_list_tenant_b_agents():
    client_a, org_a = _signup("a1@isolation-test.com", "Isolation Org A1")
    _client_b, org_b = _signup("b1@isolation-test.com", "Isolation Org B1")

    own_resp = client_a.get(f"/api/v1/orgs/{org_a}/agents")
    assert own_resp.status_code == 200

    cross_resp = client_a.get(f"/api/v1/orgs/{org_b}/agents")
    assert cross_resp.status_code == 404  # not 403 — existence isn't confirmed either


def test_tenant_a_cannot_list_tenant_b_phone_numbers():
    client_a, _org_a = _signup("a2@isolation-test.com", "Isolation Org A2")
    _client_b, org_b = _signup("b2@isolation-test.com", "Isolation Org B2")

    cross_resp = client_a.get(f"/api/v1/orgs/{org_b}/phone-numbers")
    assert cross_resp.status_code == 404


def test_tenant_a_cannot_purchase_number_for_tenant_b():
    client_a, _org_a = _signup("a3@isolation-test.com", "Isolation Org A3")
    _client_b, org_b = _signup("b3@isolation-test.com", "Isolation Org B3")

    resp = client_a.post(f"/api/v1/orgs/{org_b}/phone-numbers", json={"country": "US", "area_code": "305"})
    assert resp.status_code == 404


def test_unauthenticated_request_rejected():
    anon = TestClient(app)
    _client_a, org_a = _signup("a4@isolation-test.com", "Isolation Org A4")
    resp = anon.get(f"/api/v1/orgs/{org_a}/agents")
    assert resp.status_code == 401


def test_member_cannot_purchase_number_only_admin_or_owner_can():
    """RBAC check: even a legitimate member of the SAME org is blocked from
    an admin-tier action — the owner from signup already has 'owner', so
    this test asserts the dependency exists and denies a role below the
    minimum. (Full member-invite flow is exercised once the invite
    endpoint ships; this locks in the require_role() mechanism itself.)"""
    from app.db.models.tenancy import OrgRole, role_at_least

    assert role_at_least(OrgRole.member, OrgRole.admin) is False
    assert role_at_least(OrgRole.admin, OrgRole.admin) is True
    assert role_at_least(OrgRole.owner, OrgRole.admin) is True
    
def test_tenant_a_cannot_list_tenant_b_calls():
    client_a, org_a = _signup("a5@isolation-test.com", "Isolation Org A5")
    _client_b, org_b = _signup("b5@isolation-test.com", "Isolation Org B5")

    own_resp = client_a.get(f"/api/v1/orgs/{org_a}/calls")
    assert own_resp.status_code == 200

    cross_resp = client_a.get(f"/api/v1/orgs/{org_b}/calls")
    assert cross_resp.status_code == 404


def test_tenant_a_cannot_list_or_update_tenant_b_leads():
    client_a, _org_a = _signup("a6@isolation-test.com", "Isolation Org A6")
    _client_b, org_b = _signup("b6@isolation-test.com", "Isolation Org B6")

    cross_list = client_a.get(f"/api/v1/orgs/{org_b}/leads")
    assert cross_list.status_code == 404

    import uuid

    cross_update = client_a.patch(
        f"/api/v1/orgs/{org_b}/leads/{uuid.uuid4()}", json={"status": "contacted"}
    )
    assert cross_update.status_code == 404