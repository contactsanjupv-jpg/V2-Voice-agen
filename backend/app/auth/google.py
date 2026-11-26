"""Google OpenID Connect (authorization-code flow + PKCE) building blocks.

Nothing here touches the database or sets cookies. Secrets (the client secret, authorization codes, ID tokens, PKCE
verifiers) are never logged; failures carry a short reason code only.
"""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
import redis

from app.config import get_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
STATE_TTL_SECONDS = 600
PENDING_TTL_SECONDS = 900
JWKS_TTL_SECONDS = 3600
CLOCK_LEEWAY_SECONDS = 30

_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)


class GoogleAuthError(Exception):
    """`code` is the only thing that may be logged or shown (never provider text)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def digest(value: str) -> str:
    """SHA-256 hex of a secret cookie value, so Redis never holds a usable session id or binding secret."""
    return _digest(value)


# ---------------- state / PKCE ----------------

def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def begin_authorization(link_user_id: str | None = None, link_session: str | None = None) -> tuple[str, str]:
    """Returns (state, authorization_url). The verifier and nonce live only in Redis, keyed by a hash of the state.
    `link_user_id` marks an explicit "connect Google" started by that signed-in user, and `link_session` is the digest of the
    session it was started from; the callback requires both to still match (see google_auth.google_callback)."""
    settings = get_settings()
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    _redis.set(f"google-state:{_digest(state)}", json.dumps({"verifier": verifier, "nonce": nonce, "link_user_id": link_user_id, "link_session": link_session}), ex=STATE_TTL_SECONDS)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return state, f"{AUTH_URL}?{urlencode(params)}"


def consume_state(state: str) -> dict | None:
    """Single use: a replayed callback finds nothing."""
    raw = _redis.getdel(f"google-state:{_digest(state)}")
    return json.loads(raw) if raw else None


def states_match(cookie_state: str | None, query_state: str | None) -> bool:
    return bool(cookie_state and query_state) and hmac.compare_digest(cookie_state, query_state)


# ---------------- code exchange + ID token ----------------

def _post_token(code: str, verifier: str) -> dict:
    settings = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise GoogleAuthError("token_exchange_rejected")
    return resp.json()


def exchange_code(code: str, verifier: str) -> str:
    try:
        body = _post_token(code, verifier)
    except GoogleAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - network, TLS, bad JSON: all the same to the caller
        raise GoogleAuthError("token_exchange_failed") from exc
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise GoogleAuthError("no_id_token")
    return id_token


_jwks_lock = threading.Lock()
_jwks_cache: dict = {"fetched_at": 0.0, "keys": {}}


def _fetch_jwks() -> dict:
    resp = httpx.get(JWKS_URL, timeout=10)
    resp.raise_for_status()
    return {k["kid"]: k for k in resp.json().get("keys", []) if "kid" in k}


def _signing_key(kid: str):
    with _jwks_lock:
        fresh = time.time() - _jwks_cache["fetched_at"] < JWKS_TTL_SECONDS
        if not (fresh and kid in _jwks_cache["keys"]):
            # Unknown kid or stale cache: refetch (handles Google's key rotation), at most once per call.
            try:
                _jwks_cache["keys"], _jwks_cache["fetched_at"] = _fetch_jwks(), time.time()
            except Exception as exc:  # noqa: BLE001
                raise GoogleAuthError("jwks_unavailable") from exc
        jwk = _jwks_cache["keys"].get(kid)
    if jwk is None:
        raise GoogleAuthError("unknown_signing_key")
    return jwt.PyJWK(jwk).key


def verify_id_token(id_token: str, expected_nonce: str) -> GoogleIdentity:
    """Signature (RS256 only), issuer, audience, expiry/issued-at, nonce, and a verified email."""
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise GoogleAuthError("malformed_token") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):  # blocks alg=none and HS256 key-confusion
        raise GoogleAuthError("bad_algorithm")
    key = _signing_key(header["kid"])
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_CLIENT_ID,
            issuer=list(ISSUERS),
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise GoogleAuthError("token_expired") from exc
    except jwt.PyJWTError as exc:
        raise GoogleAuthError("token_invalid") from exc
    if not hmac.compare_digest(str(claims.get("nonce", "")), expected_nonce):
        raise GoogleAuthError("nonce_mismatch")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("no_email")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("email_not_verified")
    sub = str(claims["sub"])
    if not sub or len(sub) > 255:
        raise GoogleAuthError("token_invalid")
    return GoogleIdentity(sub=sub, email=email, name=str(claims.get("name") or "")[:255])


# ---------------- pending sign-up (verified with Google, business name still to come) ----------------

def save_pending(identity: GoogleIdentity) -> str:
    token = secrets.token_urlsafe(32)
    _redis.set(
        f"google-pending:{_digest(token)}",
        json.dumps({"sub": identity.sub, "email": identity.email, "name": identity.name}),
        ex=PENDING_TTL_SECONDS,
    )
    return token


def peek_pending(token: str) -> GoogleIdentity | None:
    raw = _redis.get(f"google-pending:{_digest(token)}")
    return GoogleIdentity(**json.loads(raw)) if raw else None


def consume_pending(token: str) -> GoogleIdentity | None:
    raw = _redis.getdel(f"google-pending:{_digest(token)}")
    return GoogleIdentity(**json.loads(raw)) if raw else None


# ---------------- email-ownership verification for linking (bound to the browser that did the Google sign-in) ----------------

LINK_VERIFY_TTL_SECONDS = 1800


def save_link_verification(pending_token: str, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _redis.set(
        f"google-linkverify:{_digest(token)}",
        json.dumps({"pending": _digest(pending_token), "user_id": user_id}),
        ex=LINK_VERIFY_TTL_SECONDS,
    )
    return token


def peek_link_verification(token: str) -> dict | None:
    raw = _redis.get(f"google-linkverify:{_digest(token)}")
    return json.loads(raw) if raw else None


def consume_link_verification(token: str) -> dict | None:
    """Atomic and single use (GETDEL): of any number of simultaneous or replayed uses, exactly one receives the record."""
    raw = _redis.getdel(f"google-linkverify:{_digest(token)}")
    return json.loads(raw) if raw else None


def pending_digest(pending_token: str) -> str:
    return _digest(pending_token)


# ---------------- email-ownership proof before a PASSWORDLESS signed-in user connects a Google account ----------------

LINK_INTENT_TTL_SECONDS = 1800


def save_link_intent(user_id: str, session_digest: str, bind_digest: str) -> str:
    """Issued when a signed-in, passwordless user asks to connect Google. The token is only ever emailed to the account's own
    address; it is bound to that user, the session it was requested from and the browser that asked (a cookie)."""
    token = secrets.token_urlsafe(32)
    _redis.set(
        f"google-linkintent:{_digest(token)}",
        json.dumps({"user_id": user_id, "session": session_digest, "bind": bind_digest}),
        ex=LINK_INTENT_TTL_SECONDS,
    )
    return token


def peek_link_intent(token: str) -> dict | None:
    raw = _redis.get(f"google-linkintent:{_digest(token)}")
    return json.loads(raw) if raw else None


def consume_link_intent(token: str) -> dict | None:
    """Atomic and single use: of two simultaneous uses, exactly one gets the record."""
    raw = _redis.getdel(f"google-linkintent:{_digest(token)}")
    return json.loads(raw) if raw else None
