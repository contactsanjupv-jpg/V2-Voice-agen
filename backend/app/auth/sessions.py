"""
Server-side sessions: the cookie carries only an opaque, signed session ID.
The actual session data (user_id, created_at, last_seen_at) lives in Redis
keyed by that ID, which is what makes instant revocation possible (logout,
"log out all devices", a detected compromise) — a bare JWT-in-cookie can't
do that without a denylist, which is just this store by another name.
"""
import json
import secrets
from datetime import datetime, timezone

import redis
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_settings

settings = get_settings()
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
_serializer = URLSafeTimedSerializer(settings.APP_SECRET_KEY, salt="atla-session-cookie")


def _redis_key(session_id: str) -> str:
    return f"session:{session_id}"


def create_session(user_id: str) -> str:
    """Returns the signed cookie value to set on the response."""
    session_id = secrets.token_urlsafe(32)
    payload = {
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _redis.set(_redis_key(session_id), json.dumps(payload), ex=settings.SESSION_TTL_SECONDS)
    return _serializer.dumps(session_id)


def read_session(cookie_value: str) -> dict | None:
    try:
        session_id = _serializer.loads(cookie_value, max_age=settings.SESSION_TTL_SECONDS)
    except BadSignature:
        return None
    raw = _redis.get(_redis_key(session_id))
    if raw is None:
        return None
    data = json.loads(raw)
    data["_session_id"] = session_id
    return data


def destroy_session(cookie_value: str) -> None:
    try:
        session_id = _serializer.loads(cookie_value, max_age=settings.SESSION_TTL_SECONDS)
    except BadSignature:
        return
    _redis.delete(_redis_key(session_id))


def destroy_all_sessions_for_user(user_id: str) -> None:
    """Used on password change / suspected compromise. O(n) scan is fine at
    our scale; move to a per-user session index if that ever changes."""
    for key in _redis.scan_iter("session:*"):
        raw = _redis.get(key)
        if raw and json.loads(raw).get("user_id") == user_id:
            _redis.delete(key)
