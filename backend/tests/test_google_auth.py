"""Continue with Google: state/PKCE, ID-token validation (real RS256 signatures), linking, new sign-ups, password-less users.

Google itself is never called: the token endpoint and the JWKS fetch are replaced, but every ID token is a genuinely
signed JWT and goes through the production verification code.
"""
import json
import logging
import time
import uuid
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

from app.auth import google as google_mod
from app.config import Settings, get_settings
from app.db.base import SessionLocal
from app.main import app
from tests.test_tenant_isolation import _signup

settings = get_settings()
CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret-value"
REDIRECT = "http://localhost:8000/api/v1/auth/google/callback"
PW = "correct-horse-battery-staple"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid):
    d = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    d.update(kid=kid, alg="RS256", use="sig")
    return d


class Google:
    """The fake provider. `token_for` builds a signed ID token for the nonce of a started flow."""

    def __init__(self):
        self.sub = f"sub-{uuid.uuid4().hex}"
        self.email = f"g-{uuid.uuid4().hex[:10]}@google-test.com"
        self.exchange_calls: list[dict] = []
        self.token_response: dict | Exception | None = None  # override the whole token-endpoint answer
        self.overrides: dict = {}
        self.key, self.kid = KEY, "k1"
        self.jwks = {"k1": _jwk(KEY, "k1")}
        self.jwks_fetches = 0

    def token_for(self, nonce, **over):
        now = int(time.time())
        claims = dict(iss="https://accounts.google.com", aud=CLIENT_ID, sub=self.sub, email=self.email,
                      email_verified=True, name="Gina Google", iat=now - 10, exp=now + 600, nonce=nonce)
        claims.update(self.overrides)
        claims.update(over)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def g(monkeypatch):
    fake = Google()
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(settings, "GOOGLE_REDIRECT_URI", REDIRECT)
    google_mod._jwks_cache.update(fetched_at=0.0, keys={})

    def fetch():
        fake.jwks_fetches += 1
        return dict(fake.jwks)

    monkeypatch.setattr(google_mod, "_fetch_jwks", fetch)

    def post_token(code, verifier):
        fake.exchange_calls.append({"code": code, "verifier": verifier})
        if isinstance(fake.token_response, Exception):
            raise fake.token_response
        if fake.token_response is not None:
            return fake.token_response
        return {"id_token": fake.token_for(fake.current_nonce)}

    monkeypatch.setattr(google_mod, "_post_token", post_token)
    fake.current_nonce = None
    return fake


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr("app.api.v1.account.send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def _start(client, g):
    r = client.get("/api/v1/auth/google/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    g.current_nonce = q["nonce"][0]
    return r, q


def _flow(client, g, **callback):
    """start -> callback, returns (callback_response, start_query)."""
    _, q = _start(client, g)
    params = {"code": "auth-code-1", "state": q["state"][0], **callback}
    params = {k: v for k, v in params.items() if v is not None}
    return client.get("/api/v1/auth/google/callback", params=params, follow_redirects=False), q


def _err(resp):
    return parse_qs(urlparse(resp.headers["location"]).query).get("google_error", [None])[0]


def _count(sql, **p):
    with SessionLocal() as db:
        return db.execute(text(sql), p).scalar()


def _has_session(client) -> bool:
    return client.get("/api/v1/auth/me").status_code == 200


# ============================ start ============================

def test_start_redirects_to_google_with_state_nonce_and_pkce(g):
    c = TestClient(app)
    r, q = _start(c, g)
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert q["client_id"] == [CLIENT_ID] and q["redirect_uri"] == [REDIRECT] and q["response_type"] == ["code"]
    assert q["scope"] == ["openid email profile"] and q["code_challenge_method"] == ["S256"]
    assert len(q["state"][0]) >= 40 and len(q["nonce"][0]) >= 40 and q["code_challenge"][0]
    assert CLIENT_SECRET not in r.headers["location"]  # the secret only ever goes server-to-server
    cookie = r.headers["set-cookie"]
    assert "atla_g_state=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/api/v1/auth/google" in cookie


def test_every_start_gets_a_fresh_state(g):
    states = {_start(TestClient(app), g)[1]["state"][0] for _ in range(3)}
    assert len(states) == 3


def test_pkce_verifier_sent_to_google_matches_the_challenge(g):
    c = TestClient(app)
    resp, q = _flow(c, g)
    assert resp.status_code == 302
    verifier = g.exchange_calls[0]["verifier"]
    assert google_mod.pkce_challenge(verifier) == q["code_challenge"][0]
    assert len(verifier) >= 43


def test_unconfigured_google_hides_the_button_and_start_fails_closed(g, monkeypatch):
    c = TestClient(app)
    assert c.get("/api/v1/auth/providers").json() == {"google": True}
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "")
    assert c.get("/api/v1/auth/providers").json() == {"google": False}
    r = c.get("/api/v1/auth/google/start", follow_redirects=False)
    assert r.status_code == 302 and _err(r) == "not_configured" and "accounts.google.com" not in r.headers["location"]
    assert _err(c.get("/api/v1/auth/google/callback", params={"code": "x", "state": "y"}, follow_redirects=False)) == "not_configured"


def test_start_is_rate_limited(g):
    c = TestClient(app)
    results = [c.get("/api/v1/auth/google/start", follow_redirects=False) for _ in range(32)]
    assert all("accounts.google.com" in r.headers["location"] for r in results[:30])
    assert _err(results[30]) == "rate_limited"


# ============================ state handling ============================

def test_callback_without_the_state_cookie_is_rejected(g):
    a, b = TestClient(app), TestClient(app)
    _, q = _start(a, g)
    r = b.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert _err(r) == "expired" and g.exchange_calls == [] and not _has_session(b)  # login-CSRF: a different browser


def test_a_tampered_or_unknown_state_is_rejected(g):
    c = TestClient(app)
    _, q = _start(c, g)
    r = c.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0] + "x"}, follow_redirects=False)
    assert _err(r) == "expired" and g.exchange_calls == []
    r = c.get("/api/v1/auth/google/callback", params={"code": "c"}, follow_redirects=False)
    assert _err(r) == "expired"


def test_a_state_can_be_used_only_once(g):
    c = TestClient(app)
    first, q = _flow(c, g)
    assert first.status_code == 302 and "google_error" not in first.headers["location"]
    c.cookies.set("atla_g_state", q["state"][0], path="/api/v1/auth/google")  # attacker replays the same callback
    again = c.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert _err(again) == "expired" and len(g.exchange_calls) == 1


def test_an_expired_state_is_rejected(g):
    c = TestClient(app)
    _, q = _start(c, g)
    import redis

    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    r.delete(f"google-state:{google_mod._digest(q['state'][0])}")
    resp = c.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert _err(resp) == "expired"


def test_cancelling_on_google_creates_nothing_and_says_so(g):
    c = TestClient(app)
    resp, _ = _flow(c, g, code=None, error="access_denied")
    assert _err(resp) == "cancelled" and g.exchange_calls == [] and not _has_session(c)
    assert _count("select count(*) from users where email=:e", e=g.email) == 0


def test_other_provider_errors_are_a_generic_failure(g):
    resp, _ = _flow(TestClient(app), g, code=None, error="server_error")
    assert _err(resp) == "failed"
    resp, _ = _flow(TestClient(app), g, code=None)  # no code at all
    assert _err(resp) == "failed"


# ============================ ID token validation ============================

def _rejected(g, expect="failed", **over):
    c = TestClient(app)
    g.overrides = over
    resp, _ = _flow(c, g)
    assert resp.status_code == 302 and _err(resp) == expect, resp.headers["location"]
    assert not _has_session(c)
    assert _count("select count(*) from users where email=:e", e=g.email) == 0
    assert "atla_g_pending" not in resp.headers.get("set-cookie", "")


def test_wrong_audience_is_rejected(g):
    _rejected(g, aud="someone-elses-client-id")


def test_wrong_issuer_is_rejected(g):
    _rejected(g, iss="https://evil.example.com")


def test_expired_token_is_rejected(g):
    _rejected(g, exp=int(time.time()) - 3600, iat=int(time.time()) - 7200)


def test_nonce_mismatch_is_rejected(g):
    _rejected(g, nonce="not-the-nonce-we-sent")


def test_missing_nonce_is_rejected(g):
    _rejected(g, nonce=None)


def test_unverified_email_is_rejected_with_its_own_message(g):
    _rejected(g, expect="unverified_email", email_verified=False)


def test_missing_email_is_rejected(g):
    _rejected(g, expect="unverified_email", email=None)


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
def test_required_claims_must_be_present(g, claim):
    _rejected(g, **{claim: None})


def test_a_token_signed_by_another_key_is_rejected(g):
    g.key = OTHER_KEY  # signed with a key Google never published, under the published kid
    _rejected(g)


def test_an_unknown_key_id_is_rejected(g):
    g.kid = "rotated-away"
    _rejected(g)


def test_alg_none_and_hs256_confusion_are_rejected(g):
    c = TestClient(app)
    for forged in (
        jwt.encode({"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1", "email": g.email,
                    "email_verified": True, "iat": 1, "exp": 9999999999}, None, algorithm="none", headers={"kid": "k1"}),
        jwt.encode({"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1", "email": g.email,
                    "email_verified": True, "iat": 1, "exp": 9999999999}, "secret-" * 8, algorithm="HS256", headers={"kid": "k1"}),
    ):
        g.token_response = {"id_token": forged}
        resp, _ = _flow(c, g)
        assert _err(resp) == "failed" and not _has_session(c)


def test_a_token_endpoint_failure_or_garbage_is_a_clean_failure(g):
    for answer in (ConnectionError("down"), {}, {"id_token": ""}, {"id_token": "not.a.jwt"}, "nonsense"):
        g.token_response = answer
        resp, _ = _flow(TestClient(app), g)
        assert _err(resp) == "failed"


def test_google_key_rotation_refetches_the_keys_once(g):
    c = TestClient(app)
    _flow(c, g)
    assert g.jwks_fetches == 1
    _flow(TestClient(app), g)  # cached
    assert g.jwks_fetches == 1
    g.key, g.kid = OTHER_KEY, "k2"  # Google rotates; the cache doesn't know k2 yet
    g.jwks = {"k2": _jwk(OTHER_KEY, "k2")}
    resp, _ = _flow(TestClient(app), g)
    assert g.jwks_fetches == 2 and _err(resp) is None


def test_secrets_never_reach_the_logs(g, caplog):
    caplog.set_level(logging.DEBUG)
    g.overrides = {"aud": "wrong"}
    _flow(TestClient(app), g)
    g.overrides = {}
    _flow(TestClient(app), g)
    text_ = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("atla"))
    assert text_  # we do log the rejection reason...
    assert "auth-code-1" not in text_ and CLIENT_SECRET not in text_ and "eyJ" not in text_ and g.email not in text_


# ============================ brand-new Google user ============================

def test_new_google_user_gets_nothing_until_they_name_the_business(g):
    c = TestClient(app)
    resp, _ = _flow(c, g)
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/get-started/google"
    assert "atla_g_pending=" in resp.headers["set-cookie"] and "atla_session" not in resp.headers["set-cookie"]
    assert _count("select count(*) from users where email=:e", e=g.email) == 0
    assert not _has_session(c)
    who = c.get("/api/v1/auth/google/pending")
    assert who.status_code == 200 and who.json() == {"email": g.email, "name": "Gina Google", "kind": "new"}


def test_completing_creates_one_owner_account_and_signs_in(g, mail):
    c = TestClient(app)
    _flow(c, g)
    pending_cookie = c.cookies.get("atla_g_pending")
    assert pending_cookie
    done = c.post("/api/v1/auth/google/complete", json={"organization_name": "  Gina's Salon  "})
    assert done.status_code == 201 and done.json()["email"] == g.email
    assert _has_session(c)
    assert _count("select count(*) from users where email=:e", e=g.email) == 1
    with SessionLocal() as db:
        row = db.execute(text(
            "select u.password_hash, u.email_verified_at, o.name, m.role from users u "
            "join organization_members m on m.user_id=u.id join organizations o on o.id=m.organization_id where u.email=:e"
        ), {"e": g.email}).one()
        assert row.password_hash is None and row.email_verified_at is not None
        assert row.name == "Gina's Salon" and str(row.role) in ("owner", "OrgRole.owner")
        ident = db.execute(text("select provider, subject from oauth_identities")).all()
        assert ("google", g.sub) in [tuple(r) for r in ident]
    assert mail == []  # a plain sign-up is not a "linked" event
    # the pending sign-up is single-use, even if the browser (or an attacker) replays the very same cookie
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "Again"}).status_code == 400
    c.cookies.set("atla_g_pending", pending_cookie, path="/api/v1/auth/google")
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "Again"}).status_code == 400
    assert _count("select count(*) from users where email=:e", e=g.email) == 1


def test_complete_without_a_pending_signup_or_with_no_name_is_refused(g):
    c = TestClient(app)
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "X"}).status_code == 400
    assert c.get("/api/v1/auth/google/pending").status_code == 404
    _flow(c, g)
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "   "}).status_code == 422
    assert c.post("/api/v1/auth/google/complete", json={}).status_code == 422
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "x" * 256}).status_code == 422
    assert _count("select count(*) from users where email=:e", e=g.email) == 0  # a refused attempt creates nothing
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "Fine"}).status_code == 201  # validation fails BEFORE the pending sign-up is spent


def test_returning_google_user_signs_straight_in_and_a_changed_google_email_changes_nothing(g):
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/complete", json={"organization_name": "Org"})
    first_email = g.email
    c2 = TestClient(app)
    g.email = f"renamed-{uuid.uuid4().hex[:6]}@google-test.com"  # same Google account (sub), new address
    resp, _ = _flow(c2, g)
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/onboarding" and "atla_session=" in resp.headers["set-cookie"]
    assert c2.get("/api/v1/auth/me").json()["email"] == first_email
    assert _count("select count(*) from users where email=:e", e=g.email) == 0  # no duplicate account


def test_the_session_is_new_on_every_google_login(g):
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/complete", json={"organization_name": "Org"})
    first = c.cookies.get("atla_session") or c.cookies.get(settings.SESSION_COOKIE_NAME)
    resp, _ = _flow(c, g)
    second = resp.headers["set-cookie"]
    assert first and first not in second


def test_logout_ends_a_google_session(g):
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/complete", json={"organization_name": "Org"})
    assert _has_session(c)
    assert c.post("/api/v1/auth/logout").status_code == 204
    assert not _has_session(c)


# ============================ existing accounts: an email match proves nothing ============================

def _password_account():
    tag = uuid.uuid4().hex[:10]
    email = f"pw-{tag}@google-test.com"
    c, org = _signup(email, f"Pw Org {tag}")
    return c, org, email


def _passwordless_account(verified: bool):
    """A user with no password (only reachable through Google or an admin tool), with an organization."""
    from datetime import datetime, timezone

    from app.db.models.tenancy import Organization, OrganizationMember, OrgRole, User

    email = f"nopw-{uuid.uuid4().hex[:10]}@google-test.com"
    with SessionLocal() as db:
        u = User(email=email, password_hash=None, email_verified_at=datetime.now(timezone.utc) if verified else None)
        db.add(u)
        db.flush()
        o = Organization(name=f"NoPw Org {uuid.uuid4().hex[:6]}")
        db.add(o)
        db.flush()
        db.add(OrganizationMember(organization_id=o.id, user_id=u.id, role=OrgRole.owner, joined_at=datetime.now(timezone.utc)))
        db.commit()
        return email, str(o.id)


def _pending(c):
    r = c.get("/api/v1/auth/google/pending")
    return r.json() if r.status_code == 200 else None


def _snapshot(email):
    with SessionLocal() as db:
        row = db.execute(text("select password_hash, email_verified_at from users where email=:e"), {"e": email}).one()
    return tuple(row)


THIRD_PARTY_EMAILS = ["owner@gmail.com", "ex-employee@bigcorp.example", "Someone@Small-Biz.example", "hr@university.edu"]


@pytest.mark.parametrize("domain_email", ["{p}@gmail.com", "{p}@bigcorp.example", "{p}@Small-Biz.example"])
def test_an_email_match_never_signs_in_links_or_changes_anything(g, mail, domain_email):
    # a password account whose address is a third-party one, and a Google account that Google says is "verified" for it
    tag = uuid.uuid4().hex[:8]
    email = domain_email.format(p=f"pw-{tag}").lower()
    old, org = _signup(email, f"Org {tag}")
    before = _snapshot(email)
    g.email = email  # Google reports email_verified=true for this address
    attacker = TestClient(app)
    resp, _ = _flow(attacker, g)
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/get-started/google"
    assert "atla_session" not in resp.headers.get("set-cookie", "") and not _has_session(attacker)  # NOT signed in
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0  # NOT linked
    assert _count("select count(*) from users where email=:e", e=email) == 1  # NOT duplicated
    assert _pending(attacker)["kind"] == "link_password"  # the only way forward is the account's password
    # nothing about the real owner changed
    assert _snapshot(email) == before and _has_session(old)
    assert TestClient(app).post("/api/v1/auth/login", json={"email": email, "password": PW}).status_code == 200
    assert mail == []


def test_complete_cannot_be_used_to_take_over_an_existing_email(g):
    old, org, email = _password_account()
    g.email = email
    attacker = TestClient(app)
    _flow(attacker, g)
    r = attacker.post("/api/v1/auth/google/complete", json={"organization_name": "Mine now"})
    assert r.status_code == 409 and not _has_session(attacker)
    assert _count("select count(*) from users where email=:e", e=email) == 1
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert _pending(attacker)["kind"] == "link_password"  # still pending, still unlinked


def test_wrong_passwords_never_link_and_are_rate_limited(g, mail):
    old, org, email = _password_account()
    g.email = email
    attacker = TestClient(app)
    _flow(attacker, g)
    for _ in range(5):
        r = attacker.post("/api/v1/auth/google/link/confirm", json={"password": "not-the-password"})
        assert r.status_code == 401 and r.json()["detail"] == "That password isn't correct."
    assert attacker.post("/api/v1/auth/google/link/confirm", json={"password": PW}).status_code == 429  # even the right one: locked out
    assert not _has_session(attacker)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert _has_session(old) and mail == []
    assert TestClient(app).post("/api/v1/auth/login", json={"email": email, "password": PW}).status_code == 200


def test_the_correct_password_links_and_changes_nothing_else(g, mail):
    old, org, email = _password_account()
    before = _snapshot(email)
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    r = c.post("/api/v1/auth/google/link/confirm", json={"password": PW})
    assert r.status_code == 200 and r.json()["email"] == email
    assert _has_session(c)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert _count("select count(*) from users where email=:e", e=email) == 1  # no duplicate account
    # session preservation: the account's existing session is untouched, and so are its password and verification state
    assert _has_session(old)
    assert _snapshot(email) == before
    assert TestClient(app).post("/api/v1/auth/login", json={"email": email, "password": PW}).status_code == 200
    # organizations, memberships and roles are exactly what they were
    orgs = c.get("/api/v1/orgs/me").json()
    assert [(o["id"], o["role"]) for o in orgs] == [(org, "owner")]
    # the owner is told (security alert), and the message says nothing was removed
    to, subject, body = mail[-1]
    assert to == email and subject == "Google sign-in was added to your Atla account" and "were not changed" in body
    # the pending sign-in was single use
    assert c.post("/api/v1/auth/google/link/confirm", json={"password": PW}).status_code == 400


def test_after_linking_google_signs_in_by_sub_even_if_the_google_email_changes(g, mail):
    old, org, email = _password_account()
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/link/confirm", json={"password": PW})
    g.email = f"changed-{uuid.uuid4().hex[:6]}@elsewhere.example"
    c2 = TestClient(app)
    resp, _ = _flow(c2, g)
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/onboarding" and _has_session(c2)
    assert c2.get("/api/v1/auth/me").json()["email"] == email


def test_a_second_google_account_with_the_same_email_cannot_ride_on_the_first_link(g, mail):
    old, org, email = _password_account()
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/link/confirm", json={"password": PW})
    g.sub = f"sub-{uuid.uuid4().hex}"  # a different Google account (e.g. a recycled address) with the same email
    c2 = TestClient(app)
    resp, _ = _flow(c2, g)
    assert not _has_session(c2) and _pending(c2)["kind"] == "link_password"


def test_a_google_account_already_connected_elsewhere_cannot_be_linked_to_a_second_user(g, mail):
    old1, org1, email1 = _password_account()
    old2, org2, email2 = _password_account()
    g.email = email1
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/link/confirm", json={"password": PW})
    # the same Google account (same sub) reports another address; it signs in as user 1 and can't be moved
    g.email = email2
    c2 = TestClient(app)
    _flow(c2, g)
    assert c2.get("/api/v1/auth/me").json()["email"] == email1


# ---- no password, email never verified: a separate email-ownership step ----

def test_passwordless_unverified_account_needs_the_mailbox_not_just_google(g, mail):
    email, org = _passwordless_account(verified=False)
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    assert _pending(c)["kind"] == "link_email" and not _has_session(c)
    assert c.post("/api/v1/auth/google/link/confirm", json={"password": PW}).status_code == 409
    assert c.post("/api/v1/auth/google/complete", json={"organization_name": "X"}).status_code == 409
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert c.post("/api/v1/auth/google/link/email").status_code == 204
    to, subject, body = mail[-1]
    assert to == email and subject == "Confirm connecting Google to your Atla account" and "/get-started/google?verify=" in body
    token = body.split("verify=")[1].split()[0]
    # still not linked until the link is used
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    # a DIFFERENT browser holding only the emailed token can't complete it (no Google sign-in there)
    stranger = TestClient(app)
    assert stranger.post("/api/v1/auth/google/link/verify", json={"token": token}).status_code == 400
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    # the browser that did the Google sign-in + the mailbox token = proven
    r = c.post("/api/v1/auth/google/link/verify", json={"token": token})
    assert r.status_code == 200 and _has_session(c)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert _snapshot(email)[1] is None  # linking never changes an existing account's verification state
    assert [(o["id"], o["role"]) for o in c.get("/api/v1/orgs/me").json()] == [(org, "owner")]
    assert c.post("/api/v1/auth/google/link/verify", json={"token": token}).status_code == 400  # single use


def test_an_emailed_token_is_bound_to_the_google_sign_in_that_requested_it(g, mail):
    email, org = _passwordless_account(verified=False)
    g.email = email
    victim_browser = TestClient(app)
    _flow(victim_browser, g)
    victim_browser.post("/api/v1/auth/google/link/email")
    token = mail[-1][2].split("verify=")[1].split()[0]
    # an attacker with a different Google account for the same email starts their own sign-in
    g.sub = f"sub-{uuid.uuid4().hex}"
    attacker = TestClient(app)
    _flow(attacker, g)
    assert attacker.post("/api/v1/auth/google/link/verify", json={"token": token}).status_code == 400
    assert attacker.post("/api/v1/auth/google/link/verify", json={"token": "x" * 43}).status_code == 400
    assert not _has_session(attacker) and _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0


def test_email_verification_requests_are_rate_limited(g, mail):
    email, org = _passwordless_account(verified=False)
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    codes = [c.post("/api/v1/auth/google/link/email").status_code for _ in range(4)]
    assert codes == [204, 204, 204, 429]


def test_a_verified_passwordless_account_cannot_be_taken_over_by_an_email_match(g, mail):
    email, org = _passwordless_account(verified=True)  # e.g. it belongs to a different Google account
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    assert _pending(c)["kind"] == "blocked" and not _has_session(c)
    for call in (
        lambda: c.post("/api/v1/auth/google/link/confirm", json={"password": PW}),
        lambda: c.post("/api/v1/auth/google/link/email"),
        lambda: c.post("/api/v1/auth/google/complete", json={"organization_name": "X"}),
    ):
        assert call().status_code == 409
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0 and mail == []


# ---- already signed in: explicit linking from Settings ----

def _start_link(c, g, password=None):
    r = c.post("/api/v1/auth/google/link/start", json={"password": password})
    if r.status_code != 200 or "url" not in r.json():
        return r, None
    q = parse_qs(urlparse(r.json()["url"]).query)
    g.current_nonce = q["nonce"][0]
    return r, q


def test_signed_in_user_with_a_password_must_re_enter_it_to_connect_google(g, mail):
    old, org, email = _password_account()
    assert TestClient(app).post("/api/v1/auth/google/link/start", json={}).status_code == 401  # not signed in
    for bad in (None, "", "wrong-password-here"):
        r, _ = _start_link(old, g, bad)
        assert r.status_code == 401
    r, q = _start_link(old, g, PW)
    assert r.status_code == 200 and "atla_g_state=" in r.headers["set-cookie"]
    # Google's page, then back: it is linked to the SIGNED-IN user, even though the Google email differs
    g.email = f"personal-{uuid.uuid4().hex[:6]}@gmail.com"
    cb = old.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"] == f"{settings.FRONTEND_URL}/dashboard/settings?google_link=ok"
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert _has_session(old) and TestClient(app).post("/api/v1/auth/login", json={"email": email, "password": PW}).status_code == 200
    assert _snapshot(email)[1] is None  # a Google claim does not "verify" the account's own address
    assert old.get("/api/v1/auth/google/status").json() == {"configured": True, "linked": True, "has_password": True}
    assert mail[-1][1] == "Google sign-in was added to your Atla account"
    # and now Google signs in as that same user
    g.email = "whatever@else.example"
    again = TestClient(app)
    _flow(again, g)
    assert again.get("/api/v1/auth/me").json()["email"] == email
    r, _ = _start_link(old, g, PW)
    assert r.status_code == 409  # one connected Google account


def test_an_explicit_link_dies_if_the_session_changes_before_the_callback(g):
    a, _, email_a = _password_account()
    b, _, email_b = _password_account()
    r, q = _start_link(a, g, PW)
    cb = b.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_error=expired" in cb.headers["location"]  # b has no state cookie: login-CSRF blocked, fails closed
    r, q = _start_link(a, g, PW)
    b.cookies.set("atla_g_state", q["state"][0], path="/api/v1/auth/google")
    cb = b.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_link=session_changed" in cb.headers["location"]
    anon = TestClient(app)
    r, q = _start_link(a, g, PW)
    anon.cookies.set("atla_g_state", q["state"][0], path="/api/v1/auth/google")
    cb = anon.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_link=session_changed" in cb.headers["location"] and not _has_session(anon)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0


def test_a_google_account_belonging_to_another_user_cannot_be_connected_explicitly(g, mail):
    a, _, email_a = _password_account()
    b, _, email_b = _password_account()
    _, q = _start_link(a, g, PW)
    a.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    _, q = _start_link(b, g, PW)  # same Google account (same sub)
    cb = b.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_link=already_linked" in cb.headers["location"]
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1


def test_cancelling_an_explicit_link_changes_nothing(g):
    a, _, email = _password_account()
    _, q = _start_link(a, g, PW)
    cb = a.get("/api/v1/auth/google/callback", params={"state": q["state"][0], "error": "access_denied"}, follow_redirects=False)
    assert "google_link=cancelled" in cb.headers["location"] and _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert _has_session(a)


def test_status_reports_the_callers_own_state_only(g):
    assert TestClient(app).get("/api/v1/auth/google/status").status_code == 401
    a, _, _ = _password_account()
    assert a.get("/api/v1/auth/google/status").json() == {"configured": True, "linked": False, "has_password": True}


def test_explicit_link_needs_google_configured(g, monkeypatch):
    a, _, _ = _password_account()
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "")
    assert a.post("/api/v1/auth/google/link/start", json={"password": PW}).status_code == 503



# ============================ signed-in PASSWORDLESS account: a session alone must never be enough ============================

def _signed_in_passwordless():
    """A signed-in user with no password (so there is no password to re-enter) and no Google account yet."""
    from app.auth.sessions import create_session

    email, org = _passwordless_account(verified=False)
    with SessionLocal() as db:
        uid = str(db.execute(text("select id from users where email=:e"), {"e": email}).scalar())
    cookie = create_session(uid)
    c = TestClient(app)
    c.cookies.set(settings.SESSION_COOKIE_NAME, cookie)
    return c, email, org, uid, cookie


def _stolen(cookie):
    """The attacker: a different browser holding a copy of the victim's session cookie, and nothing else."""
    a = TestClient(app)
    a.cookies.set(settings.SESSION_COOKIE_NAME, cookie)
    return a


def _token(mail):
    return mail[-1][2].split("google_link_verify=")[1].split()[0]


def _verify_start(c, token):
    return c.post("/api/v1/auth/google/link/verify-start", json={"token": token})


def _linked(g):
    return _count("select count(*) from oauth_identities where subject=:s", s=g.sub)


def test_passwordless_start_emails_the_mailbox_and_returns_no_google_url(g, mail):
    c, email, org, uid, cookie = _signed_in_passwordless()
    r = c.post("/api/v1/auth/google/link/start", json={"password": "anything-at-all"})  # a password is meaningless here
    assert r.status_code == 200 and r.json() == {"verification": "email_sent"}  # no URL: a session alone gets nothing
    assert "atla_g_state" not in r.headers.get("set-cookie", "") and "atla_g_linkbind=" in r.headers["set-cookie"]
    to, subject, body = mail[-1]
    assert to == email and subject == "Confirm connecting Google to your Atla account"
    assert "SAME browser" in body and "/dashboard/settings?google_link_verify=" in body
    assert _linked(g) == 0


def test_a_stolen_session_cannot_connect_the_attackers_google_account(g, mail):
    victim, email, org, uid, cookie = _signed_in_passwordless()
    attacker = _stolen(cookie)
    before = _snapshot(email)
    # 1. the attacker asks to connect Google: the link goes to the VICTIM's mailbox, the attacker gets no URL and no state
    r = attacker.post("/api/v1/auth/google/link/start", json={})
    assert r.json() == {"verification": "email_sent"} and mail[-1][0] == email
    assert "url" not in r.json() and "atla_g_state" not in attacker.cookies
    # 2. without the emailed token there is nothing to continue with
    assert attacker.post("/api/v1/auth/google/link/verify-start", json={}).status_code == 422
    for guess in ("x" * 43, "A" * 43, "not-a-real-token"):
        assert _verify_start(attacker, guess).status_code == 400
    assert "atla_g_state" not in attacker.cookies
    # 3. a forged callback (state the attacker made up, or one cookie they set themselves) is rejected
    attacker.cookies.set("atla_g_state", "forged", path="/api/v1/auth/google")
    g.current_nonce = "n"
    cb = attacker.get("/api/v1/auth/google/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert "google_error=expired" in cb.headers["location"] and g.exchange_calls == []
    # 4. even holding the emailed token (e.g. a leaked mailbox link) the attacker's browser lacks the bind cookie of the browser
    #    that asked, or, if the attacker asked, the victim's mailbox owns the token
    assert _linked(g) == 0 and _snapshot(email) == before
    assert victim.get("/api/v1/auth/me").status_code == 200  # the victim's session was never touched


def test_the_emailed_link_is_useless_in_any_other_browser_and_is_not_burned_by_trying(g, mail):
    victim, email, org, uid, cookie = _signed_in_passwordless()
    victim.post("/api/v1/auth/google/link/start", json={})
    token = _token(mail)
    stranger = _stolen(cookie)  # same session cookie (stolen), but it never held the bind cookie
    assert _verify_start(stranger, token).status_code == 400
    assert "atla_g_state" not in stranger.cookies and _linked(g) == 0
    # the failed attempt consumed nothing: the real browser can still use it
    ok = _verify_start(victim, token)
    assert ok.status_code == 200 and "accounts.google.com" in ok.json()["url"]
    assert "atla_g_state=" in ok.headers["set-cookie"]  # the state cookie is minted only now, in the verifying browser
    assert "atla_g_linkbind" in ok.headers["set-cookie"] and "Max-Age=0" in ok.headers["set-cookie"]  # bind cookie retired


def test_an_attacker_started_token_cannot_be_used_in_the_victims_browser(g, mail):
    victim, email, org, uid, cookie = _signed_in_passwordless()
    attacker = _stolen(cookie)
    attacker.post("/api/v1/auth/google/link/start", json={})  # attacker's browser holds the bind cookie, the victim gets the email
    token = _token(mail)
    assert _verify_start(victim, token).status_code == 400  # the victim's browser never held that bind cookie
    assert _verify_start(attacker, token).status_code == 200  # only the browser that asked, with the mailbox's token, proceeds
    # ...which is why the mailbox is the real gate: an attacker without mailbox access never sees `token`.


def test_the_verification_is_single_use_and_expires(g, mail):
    c, email, org, uid, cookie = _signed_in_passwordless()
    c.post("/api/v1/auth/google/link/start", json={})
    token = _token(mail)
    bind = c.cookies.get("atla_g_linkbind")
    assert _verify_start(c, token).status_code == 200
    assert _verify_start(c, token).status_code == 400  # replay (the first success also retired the bind cookie)
    c.cookies.set("atla_g_linkbind", bind, path="/api/v1/auth/google")  # ...and even with every secret replayed, it is spent
    assert _verify_start(c, token).status_code == 400
    # expiry
    c2, email2, _, _, cookie2 = _signed_in_passwordless()
    c2.post("/api/v1/auth/google/link/start", json={})
    token2 = _token(mail)
    google_mod._redis.delete(f"google-linkintent:{google_mod._digest(token2)}")  # what the TTL does after 30 minutes
    assert _verify_start(c2, token2).status_code == 400
    ttl_key = f"google-linkintent:{google_mod._digest(_token_after_start(c2, mail))}"
    assert 0 < google_mod._redis.ttl(ttl_key) <= google_mod.LINK_INTENT_TTL_SECONDS


def _token_after_start(c, mail):
    c.post("/api/v1/auth/google/link/start", json={})
    return _token(mail)


def test_the_verification_is_bound_to_the_user_and_to_the_session_that_asked(g, mail):
    from app.auth.sessions import create_session

    c, email, org, uid, cookie = _signed_in_passwordless()
    c.post("/api/v1/auth/google/link/start", json={})
    token = _token(mail)
    # another signed-in user (with the token somehow) can't use it
    other, _, _ = _password_account()
    other.cookies.set("atla_g_linkbind", c.cookies.get("atla_g_linkbind"), path="/api/v1/auth/google")
    assert _verify_start(other, token).status_code == 400
    # the same browser, same user, but a NEW session (logged out and in again) can't either: it must start over
    c.cookies.set(settings.SESSION_COOKIE_NAME, create_session(uid))
    assert _verify_start(c, token).status_code == 400
    assert _verify_start(TestClient(app), token).status_code == 401  # not signed in at all
    assert _linked(g) == 0


def test_the_callback_rechecks_the_session_and_user_that_started_it(g, mail):
    from app.auth.sessions import create_session

    c, email, org, uid, cookie = _signed_in_passwordless()
    c.post("/api/v1/auth/google/link/start", json={})
    url = _verify_start(c, _token(mail)).json()["url"]
    q = parse_qs(urlparse(url).query)
    g.current_nonce = q["nonce"][0]
    # the session changes before Google returns (same user, new session): refused
    c.cookies.set(settings.SESSION_COOKIE_NAME, create_session(uid))
    cb = c.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_link=session_changed" in cb.headers["location"] and _linked(g) == 0
    # the state was consumed by that attempt, so it can't be retried either
    c.cookies.set(settings.SESSION_COOKIE_NAME, cookie)
    cb = c.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_error=expired" in cb.headers["location"] and _linked(g) == 0


def test_the_google_state_from_the_verified_step_only_works_in_the_browser_that_verified(g, mail):
    c, email, org, uid, cookie = _signed_in_passwordless()
    c.post("/api/v1/auth/google/link/start", json={})
    q = parse_qs(urlparse(_verify_start(c, _token(mail)).json()["url"]).query)
    g.current_nonce = q["nonce"][0]
    elsewhere = _stolen(cookie)  # opens the Google URL / callback in another browser
    cb = elsewhere.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_error=expired" in cb.headers["location"] and _linked(g) == 0


def test_the_full_verified_path_links_and_changes_nothing_else(g, mail):
    c, email, org, uid, cookie = _signed_in_passwordless()
    before = _snapshot(email)
    c.post("/api/v1/auth/google/link/start", json={})
    q = parse_qs(urlparse(_verify_start(c, _token(mail)).json()["url"]).query)
    g.current_nonce = q["nonce"][0]
    g.email = f"personal-{uuid.uuid4().hex[:6]}@gmail.com"
    cb = c.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"] == f"{settings.FRONTEND_URL}/dashboard/settings?google_link=ok"
    assert _linked(g) == 1
    assert _snapshot(email) == before  # no password added or removed, verification state untouched
    assert c.get("/api/v1/auth/me").status_code == 200  # the session survived
    assert _stolen(cookie).get("/api/v1/auth/me").status_code == 200  # and so does every other session
    assert mail[-1][1] == "Google sign-in was added to your Atla account" and "were not changed" in mail[-1][2]
    assert [(o["id"], o["role"]) for o in c.get("/api/v1/orgs/me").json()] == [(org, "owner")]
    again = TestClient(app)
    _flow(again, g)  # now Google signs in as that user, by sub
    assert again.get("/api/v1/auth/me").json()["email"] == email


def test_passwordless_verification_is_rate_limited_and_skipped_when_already_linked(g, mail):
    c, email, org, uid, cookie = _signed_in_passwordless()
    codes = [c.post("/api/v1/auth/google/link/start", json={}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    c2, email2, _, uid2, cookie2 = _signed_in_passwordless()
    with SessionLocal() as db:
        db.execute(text("insert into oauth_identities (id, user_id, provider, subject, email, created_at, updated_at) "
                        "values (gen_random_uuid(), :u, 'google', :s, :e, now(), now())"), {"u": uid2, "s": f"sub-{uuid.uuid4().hex}", "e": email2})
        db.commit()
    sent = len(mail)
    assert c2.post("/api/v1/auth/google/link/start", json={}).status_code == 409 and len(mail) == sent  # no email either


def test_a_passwordless_account_that_gained_a_password_must_use_the_password_path(g, mail):
    from app.auth.passwords import hash_password

    c, email, org, uid, cookie = _signed_in_passwordless()
    c.post("/api/v1/auth/google/link/start", json={})
    token = _token(mail)
    with SessionLocal() as db:
        db.execute(text("update users set password_hash=:h where id=:u"), {"h": hash_password(PW), "u": uid})
        db.commit()
    assert _verify_start(c, token).status_code == 409 and _linked(g) == 0


def test_password_accounts_still_need_their_password_and_never_use_the_email_path(g, mail):
    old, org, email = _password_account()
    for bad in (None, "", "wrong-password-here"):
        assert _start_link(old, g, bad)[0].status_code == 401
    assert mail == [] and "atla_g_linkbind" not in old.cookies  # no email, no bind cookie for password accounts
    r, q = _start_link(old, g, PW)
    assert r.status_code == 200 and q
    # same-user, different-session callback is refused for password accounts too
    from app.auth.sessions import create_session

    with SessionLocal() as db:
        uid = str(db.execute(text("select id from users where email=:e"), {"e": email}).scalar())
    old.cookies.set(settings.SESSION_COOKIE_NAME, create_session(uid))
    cb = old.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_link=session_changed" in cb.headers["location"] and _linked(g) == 0


# ============================ password-less (Google-only) users ============================

def test_a_google_only_user_cannot_password_login_change_or_delete_but_can_set_a_password(g, mail):
    c = TestClient(app)
    _flow(c, g)
    c.post("/api/v1/auth/google/complete", json={"organization_name": "G Org"})
    anon = TestClient(app)
    for pw in ("", "x", PW, "None", "null"):
        assert anon.post("/api/v1/auth/login", json={"email": g.email, "password": pw}).status_code == 401
    r = c.post("/api/v1/auth/password/change", json={"current_password": PW, "new_password": "a-brand-new-passphrase"})
    assert r.status_code == 400 and "Forgot your password" in r.json()["detail"]
    org = c.get("/api/v1/orgs/me").json()[0]["id"]
    r = c.post(f"/api/v1/orgs/{org}/delete", json={"confirm_name": "G Org", "password": PW})
    assert r.status_code == 400 and "Forgot your password" in r.json()["detail"]
    # ...and the escape hatch works end to end
    anon.post("/api/v1/auth/password-reset/request", json={"email": g.email})
    import re

    token = re.search(r"token=([\w-]+)", next(m for m in mail if m[1].startswith("Reset"))[2]).group(1)
    assert anon.post("/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "a-brand-new-passphrase"}).status_code == 204
    assert TestClient(app).post("/api/v1/auth/login", json={"email": g.email, "password": "a-brand-new-passphrase"}).status_code == 200


# ============================ configuration safety ============================

GOOD = dict(
    ENV="production", APP_SECRET_KEY="x" * 40, DATABASE_URL="postgresql+psycopg://u:p@h/db",
    FRONTEND_URL="https://app.example.com", BACKEND_PUBLIC_URL="https://api.example.com",
    PADDLE_ENV="live", RETELL_API_KEY="k", PADDLE_API_KEY="k", PADDLE_WEBHOOK_SECRET="s", PADDLE_STARTER_PRICE_ID="pri_1",
    PADDLE_GROWTH_PRICE_ID="pri_2", TOKEN_ENCRYPTION_KEY="t", SMTP_HOST="smtp.example.com",
)


def _prod(**over):
    return Settings(_env_file=None, **{**GOOD, **over}).production_config_problems()


def test_production_rejects_half_configured_or_insecure_google_settings():
    assert _prod() == []  # Google is optional
    full = dict(GOOGLE_CLIENT_ID="i", GOOGLE_CLIENT_SECRET="s", GOOGLE_REDIRECT_URI="https://api.example.com/api/v1/auth/google/callback")
    assert _prod(**full) == []
    for missing in full:
        problems = _prod(**{**full, missing: ""})
        assert len(problems) == 1 and problems[0].startswith("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REDIRECT_URI")
        assert "s" != problems[0]  # names only, never values
    http = _prod(**{**full, "GOOGLE_REDIRECT_URI": "http://api.example.com/cb"})
    assert len(http) == 1 and http[0].startswith("GOOGLE_REDIRECT_URI")


# ---- /google/link/verify: atomic single use under concurrency and replay ----

VERIFY = "/api/v1/auth/google/link/verify"


def _verify_ready(g, mail):
    """A password-less account, a Google sign-in in browser `c`, and the emailed token. Returns (c, email, token)."""
    email, _org = _passwordless_account(verified=False)
    g.email = email
    c = TestClient(app)
    _flow(c, g)
    assert c.post("/api/v1/auth/google/link/email").status_code == 204
    return c, email, mail[-1][2].split("verify=")[1].split()[0]


def _clone(c):
    """Another client (own connection) holding exactly the same cookies, i.e. the same browser sending parallel requests."""
    other = TestClient(app)
    for ck in c.cookies.jar:
        other.cookies.set(ck.name, ck.value, domain=ck.domain, path=ck.path)
    return other


def _alerts(mail, email):
    return [m for m in mail if m[0] == email and m[1] != "Confirm connecting Google to your Atla account"]


def test_concurrent_verification_attempts_yield_exactly_one_success(g, mail):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c, email, token = _verify_ready(g, mail)
    n = 6
    clients = [_clone(c) for _ in range(n)]
    gate = Barrier(n)

    def attempt(client):
        gate.wait()
        return client.post(VERIFY, json={"token": token}).status_code

    with ThreadPoolExecutor(n) as pool:
        codes = list(pool.map(attempt, clients))
    assert sorted(codes) == [200] + [400] * (n - 1), codes
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert _count("select count(*) from users where email=:e", e=email) == 1
    assert sum(1 for cl in clients if _has_session(cl)) == 1  # only the winner got a session
    assert len(_alerts(mail, email)) == 1


def test_a_replayed_verification_is_refused_even_with_the_cookies_restored(g, mail):
    c, email, token = _verify_ready(g, mail)
    replay = _clone(c)  # copy taken before the first use
    assert c.post(VERIFY, json={"token": token}).status_code == 200
    assert replay.post(VERIFY, json={"token": token}).status_code == 400
    assert not _has_session(replay)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert len(_alerts(mail, email)) == 1


def test_a_stale_peek_cannot_beat_the_atomic_consumption(g, mail, monkeypatch):
    # Simulates losing the race: the request already passed its peek, but the token is spent before it consumes.
    c, email, token = _verify_ready(g, mail)
    stale = google_mod.peek_link_verification(token)
    assert stale is not None
    assert google_mod.consume_link_verification(token) == stale  # the "winner" spends it
    monkeypatch.setattr(google_mod, "peek_link_verification", lambda t: stale)
    assert c.post(VERIFY, json={"token": token}).status_code == 400
    assert not _has_session(c) and _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0


def test_a_stale_pending_identity_cannot_be_attached_twice(g, mail, monkeypatch):
    # The token is valid but another request has already spent the pending Google identity in this browser.
    c, email, token = _verify_ready(g, mail)
    pending = c.cookies.get("atla_g_pending")
    ident = google_mod.peek_pending(pending)
    assert google_mod.consume_pending(pending) == ident
    monkeypatch.setattr(google_mod, "peek_pending", lambda t: ident)
    assert c.post(VERIFY, json={"token": token}).status_code == 400
    assert not _has_session(c) and _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert _alerts(mail, email) == []


@pytest.mark.parametrize("tamper", [{"user_id": str(uuid.uuid4())}, {"pending": "0" * 64}])
def test_a_consumed_record_for_another_user_or_identity_is_not_attached(g, mail, monkeypatch, tamper):
    c, email, token = _verify_ready(g, mail)
    real = google_mod.consume_link_verification
    monkeypatch.setattr(google_mod, "consume_link_verification", lambda t: {**real(t), **tamper})
    assert c.post(VERIFY, json={"token": token}).status_code == 400
    assert not _has_session(c) and _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert _alerts(mail, email) == []


def test_a_different_pending_identity_than_the_one_checked_is_not_attached(g, mail, monkeypatch):
    c, email, token = _verify_ready(g, mail)
    other = google_mod.GoogleIdentity(sub="sub-other", email=email, name="Other")
    monkeypatch.setattr(google_mod, "consume_pending", lambda t: other)
    assert c.post(VERIFY, json={"token": token}).status_code == 400
    assert not _has_session(c) and _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0


# ---- one Google account per Atla user: enforced by the database, safe under races ----

CONFIRM = "/api/v1/auth/google/link/confirm"


def _two_google_sign_ins(g, email):
    """Two browsers, each holding a pending sign-in from a DIFFERENT Google account, both for `email`."""
    browsers, subs = [], []
    for _ in range(2):
        g.sub, g.email = f"sub-{uuid.uuid4().hex}", email
        b = TestClient(app)
        _flow(b, g)
        assert _pending(b)["kind"] == "link_password"
        browsers.append(b)
        subs.append(g.sub)
    return browsers, subs


def _race(browsers, path, body, rendezvous_patch=None, monkeypatch=None):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    gate = Barrier(len(browsers))
    if rendezvous_patch:
        # Force the worst case: every request passes the application-level "does this user already have Google?" check
        # before any of them writes, so only the database can stop the second one.
        meet = Barrier(len(browsers), timeout=10)
        from app.api.v1 import google_auth as ga

        def blind(db, user_id):
            meet.wait()
            return None

        monkeypatch.setattr(ga, "_identity_for_user", blind)

    def go(b):
        gate.wait()
        return b.post(path, json=body)

    with ThreadPoolExecutor(len(browsers)) as pool:
        return list(pool.map(go, browsers))


@pytest.mark.parametrize("forced", [False, True], ids=["natural-race", "both-pass-the-check"])
def test_two_different_google_accounts_racing_for_one_user_link_exactly_one(g, mail, monkeypatch, forced):
    _c, org, email = _password_account()
    browsers, subs = _two_google_sign_ins(g, email)
    before = _snapshot(email)
    results = _race(browsers, CONFIRM, {"password": PW}, rendezvous_patch=forced, monkeypatch=monkeypatch)
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"] == "A different Google account is already connected to this Atla account."
    # the database holds exactly one Google identity for the user, and it is the winner's
    with SessionLocal() as db:
        rows = db.execute(text("select i.subject from oauth_identities i join users u on u.id=i.user_id where u.email=:e"), {"e": email}).all()
    assert len(rows) == 1 and rows[0][0] in subs
    winner = results.index(next(r for r in results if r.status_code == 200))
    assert rows[0][0] == subs[winner]
    assert _has_session(browsers[winner]) and not _has_session(browsers[1 - winner])  # the rejected one gets no session
    assert _count("select count(*) from oauth_identities where subject=:s", s=subs[1 - winner]) == 0
    assert _snapshot(email) == before  # password and verification state untouched
    assert len(_alerts(mail, email)) == 1  # one "Google was added" notice, none for the rejected attempt


def test_a_different_google_account_is_refused_once_one_is_connected(g, mail):
    _c, org, email = _password_account()
    browsers, subs = _two_google_sign_ins(g, email)
    assert browsers[0].post(CONFIRM, json={"password": PW}).status_code == 200
    r = browsers[1].post(CONFIRM, json={"password": PW})
    assert r.status_code == 409 and "different Google account is already connected to this Atla account" in r.json()["detail"]
    assert not _has_session(browsers[1])
    assert _count("select count(*) from oauth_identities where subject=:s", s=subs[1]) == 0


def test_the_explicit_link_callback_names_which_conflict_it_hit(g, mail, monkeypatch):
    # Another Google account gets connected to the user between "start" and the callback, and this request's application
    # check misses it (as under a real race): the database constraint refuses, and the message says why.
    from app.api.v1 import google_auth as ga

    a, _, email = _password_account()
    _, q = _start_link(a, g, PW)
    with SessionLocal() as db:
        uid = db.execute(text("select id from users where email=:e"), {"e": email}).scalar()
        db.execute(text("insert into oauth_identities (id,user_id,provider,subject,email,created_at,updated_at) "
                        "values (gen_random_uuid(),:u,'google',:s,'x@y.z',now(),now())"), {"u": uid, "s": f"sub-{uuid.uuid4().hex}"})
        db.commit()
    monkeypatch.setattr(ga, "_identity_for_user", lambda db, user_id: None)
    cb = a.get("/api/v1/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert "google_link=already_connected" in cb.headers["location"]
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0


def test_the_database_itself_allows_one_identity_per_user_and_per_google_account(g):
    from sqlalchemy.exc import IntegrityError

    _c, _o, e1 = _password_account()
    _c2, _o2, e2 = _password_account()
    ins = text("insert into oauth_identities (id,user_id,provider,subject,email,created_at,updated_at) "
               "select gen_random_uuid(), id, 'google', :s, 'x@y.z', now(), now() from users where email=:e")
    s1, s2 = f"sub-{uuid.uuid4().hex}", f"sub-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        db.execute(ins, {"s": s1, "e": e1})
        db.commit()
    with SessionLocal() as db:  # a second, different Google account for the SAME user
        with pytest.raises(IntegrityError, match="uq_oauth_user_provider"):
            db.execute(ins, {"s": s2, "e": e1})
            db.commit()
    with SessionLocal() as db:  # the SAME Google account for a DIFFERENT user (the pre-existing constraint is still there)
        with pytest.raises(IntegrityError, match="uq_oauth_provider_subject"):
            db.execute(ins, {"s": s1, "e": e2})
            db.commit()
    with SessionLocal() as db:  # a different Google account for a different user is fine
        db.execute(ins, {"s": s2, "e": e2})
        db.commit()


# ---- /google/link/confirm: the pending Google identity is single use, atomically ----

def _confirm_ready(g):
    c, org, email = _password_account()
    g.email = email
    b = TestClient(app)
    _flow(b, g)
    assert _pending(b)["kind"] == "link_password"
    return b, email


def test_concurrent_confirms_with_the_same_pending_identity_link_exactly_once(g, mail):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    b, email = _confirm_ready(g)
    n = 4
    clients = [_clone(b) for _ in range(n)]  # one browser's cookies, several simultaneous requests
    gate = Barrier(n)

    def go(client):
        gate.wait()
        return client.post(CONFIRM, json={"password": PW})

    before = _snapshot(email)
    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(go, clients))
    assert sorted(r.status_code for r in results) == [200] + [400] * (n - 1), [r.text for r in results]
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert _count("select count(*) from users where email=:e", e=email) == 1
    assert sum(1 for cl in clients if _has_session(cl)) == 1  # only the winner is signed in
    assert len(_alerts(mail, email)) == 1  # one security alert, not one per request
    assert all("expired" in r.json()["detail"] for r in results if r.status_code == 400)
    assert _snapshot(email) == before


def test_a_replayed_confirm_is_refused_even_with_the_cookies_restored(g, mail):
    b, email = _confirm_ready(g)
    replay = _clone(b)
    assert b.post(CONFIRM, json={"password": PW}).status_code == 200
    assert replay.post(CONFIRM, json={"password": PW}).status_code == 400
    assert not _has_session(replay)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert len(_alerts(mail, email)) == 1


def test_a_confirm_that_loses_the_consumption_attaches_nothing(g, mail, monkeypatch):
    # The request passed every check on a peek, but the pending identity is spent before it can consume it.
    b, email = _confirm_ready(g)
    pending = b.cookies.get("atla_g_pending")
    ident = google_mod.peek_pending(pending)
    assert google_mod.consume_pending(pending) == ident  # the "winner"
    monkeypatch.setattr(google_mod, "peek_pending", lambda t: ident)
    r = b.post(CONFIRM, json={"password": PW})
    assert r.status_code == 400 and not _has_session(b)
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert _alerts(mail, email) == []


def test_a_confirm_that_consumes_a_different_identity_attaches_nothing(g, mail, monkeypatch):
    b, email = _confirm_ready(g)
    other = google_mod.GoogleIdentity(sub="sub-other", email=email, name="Other")
    monkeypatch.setattr(google_mod, "consume_pending", lambda t: other)
    r = b.post(CONFIRM, json={"password": PW})
    assert r.status_code == 400 and not _has_session(b)
    assert _count("select count(*) from oauth_identities where subject in (:a,:b)", a=g.sub, b="sub-other") == 0
    assert _alerts(mail, email) == []


def test_a_wrong_password_does_not_spend_the_pending_identity_so_the_user_can_retry(g, mail):
    b, email = _confirm_ready(g)
    r = b.post(CONFIRM, json={"password": "definitely-the-wrong-one"})
    assert r.status_code == 401 and not _has_session(b)
    assert _pending(b)["kind"] == "link_password"  # still waiting
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 0
    assert b.post(CONFIRM, json={"password": PW}).status_code == 200
    assert _count("select count(*) from oauth_identities where subject=:s", s=g.sub) == 1
    assert len(_alerts(mail, email)) == 1
