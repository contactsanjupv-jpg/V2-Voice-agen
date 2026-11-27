"""
Task 11 adversarial probes: black-box checks over real HTTP (TestClient) against the live test DB/Redis.
Each probe states the attack it stands in for. These are regression guards, not proof of absence.
"""
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.test_tenant_isolation import _signup

PW = "correct-horse-battery-staple"


def _email() -> str:
    return f"t11-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture(autouse=True)
def _clear_limits():
    import redis

    r = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    for k in r.scan_iter("ratelimit:*"):
        r.delete(k)
    yield


def test_session_cookie_flags():
    c = TestClient(app)
    r = c.post("/api/v1/auth/signup", json={"email": _email(), "password": PW, "organization_name": f"T11 {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "path=/" in sc


def test_forged_and_tampered_session_cookies_are_rejected():
    name = get_settings().SESSION_COOKIE_NAME
    for bad in ("", "x", "a.b.c", "../../etc/passwd", "A" * 4096):
        c = TestClient(app)
        c.cookies.set(name, bad)
        assert c.get("/api/v1/auth/me").status_code == 401
    # a cookie signed with a different secret is not accepted
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer("not-the-real-secret-key-0123456789abcdef", salt="atla-session-cookie").dumps("anything")
    c = TestClient(app)
    c.cookies.set(name, forged)
    assert c.get("/api/v1/auth/me").status_code == 401


def test_validly_signed_cookie_for_a_nonexistent_server_session_is_rejected():
    from app.auth.sessions import _serializer

    c = TestClient(app)
    c.cookies.set(get_settings().SESSION_COOKIE_NAME, _serializer.dumps("no-such-session-id"))
    assert c.get("/api/v1/auth/me").status_code == 401


def test_logout_revokes_the_server_side_session_even_if_the_cookie_is_replayed():
    c, _ = _signup(_email(), f"T11 {uuid.uuid4().hex[:6]}")
    cookie = c.cookies.get(get_settings().SESSION_COOKIE_NAME)
    assert c.get("/api/v1/auth/me").status_code == 200
    assert c.post("/api/v1/auth/logout").status_code in (200, 204)
    replay = TestClient(app)
    replay.cookies.set(get_settings().SESSION_COOKIE_NAME, cookie)
    assert replay.get("/api/v1/auth/me").status_code == 401


def test_cross_site_form_posts_cannot_drive_state_changing_endpoints():
    """CSRF: a cross-site <form> can only send simple content types. Endpoints must not accept them as JSON."""
    c, org = _signup(_email(), f"T11 {uuid.uuid4().hex[:6]}")
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        r = c.post("/api/v1/auth/password/change", content='{"current_password":"%s","new_password":"another-long-passphrase-1"}' % PW,
                   headers={"Content-Type": ctype, "Origin": "https://evil.example"})
        assert r.status_code in (400, 415, 422), (ctype, r.status_code)
    r = c.put(f"/api/v1/orgs/{org}/notifications/preferences/calls", content='{"enabled":false}', headers={"Content-Type": "text/plain"})
    assert r.status_code in (400, 415, 422)
    assert c.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x"}).status_code in (401, 422, 429)
    # the password was not changed
    ok = TestClient(app).post("/api/v1/auth/login", json={"email": c.get("/api/v1/auth/me").json()["email"], "password": PW})
    assert ok.status_code == 200


def test_cors_only_allows_the_configured_frontend_origin():
    c = TestClient(app)
    allowed = get_settings().FRONTEND_URL
    ok = c.options("/api/v1/auth/me", headers={"Origin": allowed, "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == allowed
    for evil in ("https://evil.example", "null", allowed + ".evil.example", "http://localhost:1"):
        r = c.options("/api/v1/auth/me", headers={"Origin": evil, "Access-Control-Request-Method": "GET"})
        assert r.headers.get("access-control-allow-origin") != evil
        assert r.headers.get("access-control-allow-origin") in (None, allowed)
        g = c.get("/api/v1/auth/providers", headers={"Origin": evil})
        assert g.headers.get("access-control-allow-origin") in (None, allowed)


def test_security_headers_on_api_responses():
    r = TestClient(app).get("/health")
    h = {k.lower(): v for k, v in r.headers.items()}
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "referrer-policy" in h


def test_login_does_not_reveal_whether_an_account_exists():
    email = _email()
    TestClient(app).post("/api/v1/auth/signup", json={"email": email, "password": PW, "organization_name": f"T11 {uuid.uuid4().hex[:6]}"})
    wrong_pw = TestClient(app).post("/api/v1/auth/login", json={"email": email, "password": "wrong-password-123"})
    no_user = TestClient(app).post("/api/v1/auth/login", json={"email": _email(), "password": "wrong-password-123"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


def test_password_reset_request_answers_identically_for_known_and_unknown_addresses():
    known = _email()
    TestClient(app).post("/api/v1/auth/signup", json={"email": known, "password": PW, "organization_name": f"T11 {uuid.uuid4().hex[:6]}"})
    a = TestClient(app).post("/api/v1/auth/password-reset/request", json={"email": known})
    b = TestClient(app).post("/api/v1/auth/password-reset/request", json={"email": _email()})
    assert a.status_code == b.status_code == 204
    assert a.content == b.content


def test_reset_token_is_single_use_unguessable_and_garbage_is_refused():
    from app.auth.reset_tokens import consume_reset_token, create_reset_token

    c, _ = _signup(_email(), f"T11 {uuid.uuid4().hex[:6]}")
    uid = c.get("/api/v1/auth/me").json()["id"]
    tok = create_reset_token(uid)
    assert len(tok) >= 40
    assert TestClient(app).post("/api/v1/auth/password-reset/confirm", json={"token": tok, "new_password": "brand-new-passphrase-77"}).status_code == 204
    assert consume_reset_token(tok) is None  # replay
    assert TestClient(app).post("/api/v1/auth/password-reset/confirm", json={"token": tok, "new_password": "yet-another-passphrase-88"}).status_code == 400
    for junk in ("", "x", "' OR 1=1 --", "A" * 5000):
        assert TestClient(app).post("/api/v1/auth/password-reset/confirm", json={"token": junk, "new_password": "brand-new-passphrase-77"}).status_code in (400, 422)
    # the old session (c) was revoked by the reset
    assert c.get("/api/v1/auth/me").status_code == 401


def test_a_new_reset_token_invalidates_the_previous_one():
    from app.auth.reset_tokens import consume_reset_token, create_reset_token

    uid = str(uuid.uuid4())
    first, second = create_reset_token(uid), create_reset_token(uid)
    assert consume_reset_token(first) is None
    assert consume_reset_token(second) == uid


def test_malformed_ids_and_injection_strings_yield_client_errors_not_500s_and_no_internals():
    c, org = _signup(_email(), f"T11 {uuid.uuid4().hex[:6]}")
    probes = [
        f"/api/v1/orgs/{org}/calls/not-a-uuid", f"/api/v1/orgs/{org}/calls/{uuid.uuid4()}",
        "/api/v1/orgs/not-a-uuid/calls", f"/api/v1/orgs/{org}/calls?limit=-1", f"/api/v1/orgs/{org}/calls?limit=99999999",
        f"/api/v1/orgs/{org}/calls/search?q=%27%3B%20DROP%20TABLE%20calls%3B--", f"/api/v1/orgs/{org}/leads?status=%00",
        f"/api/v1/orgs/{org}/notifications?after=abc", f"/api/v1/orgs/{org}/notifications?category=../../x",
    ]
    for p in probes:
        r = c.get(p)
        assert r.status_code < 500, (p, r.status_code, r.text[:200])
        low = r.text.lower()
        assert "traceback" not in low and "sqlalchemy" not in low and "psycopg" not in low, p


def test_expensive_and_money_endpoints_refuse_unsubscribed_orgs_before_any_provider_call():
    c, org = _signup(_email(), f"T11 {uuid.uuid4().hex[:6]}")
    r = c.post(f"/api/v1/orgs/{org}/phone-numbers", json={"area_code": "415"})
    assert r.status_code in (402, 403, 422), r.status_code
    r = c.post(f"/api/v1/orgs/{org}/activate")
    assert r.status_code in (402, 403, 409, 422)


def test_pyjwt_is_at_or_above_the_first_release_without_known_advisories():
    """Dependency hygiene (pip-audit, Nov 2026): PyJWT verifies the Google ID token, so it must not stay on a vulnerable release."""
    import importlib.metadata as md
    import pathlib

    pin = re.search(r"^PyJWT(?:\[[^\]]*\])?==([\d.]+)", pathlib.Path(__file__).resolve().parents[1].joinpath("requirements.txt").read_text(), re.M)
    assert pin, "PyJWT must stay pinned"
    as_tuple = lambda v: tuple(int(x) for x in v.split("."))  # noqa: E731
    assert as_tuple(pin.group(1)) >= (2, 15, 1)
    assert as_tuple(md.version("PyJWT")) >= (2, 15, 1)


def test_login_rejects_oversized_passwords_before_any_hashing():
    """A multi-MB 'password' must be refused by validation (422), never fed to Argon2 (CPU/memory abuse on an unauthenticated endpoint)."""
    import time

    c = TestClient(app)
    for n in (129, 100_000, 2_000_000):
        t = time.monotonic()
        r = c.post("/api/v1/auth/login", json={"email": "someone@example.com", "password": "a" * n})
        assert r.status_code == 422, (n, r.status_code)
        assert time.monotonic() - t < 2.0
    # a normal wrong password is still an ordinary 401
    assert c.post("/api/v1/auth/login", json={"email": "someone@example.com", "password": "a" * 128}).status_code == 401
