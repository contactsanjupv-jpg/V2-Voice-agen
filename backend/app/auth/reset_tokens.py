"""Single-use password-reset tokens. Only a SHA-256 of the token is stored (Redis),
so a leaked datastore can't be used to reset anyone's password. Requesting a new
token invalidates the previous one."""
import hashlib
import secrets

import redis

from app.config import get_settings

settings = get_settings()
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)


def _key(token: str) -> str:
    return "pwreset:" + hashlib.sha256(token.encode()).hexdigest()


def create_reset_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    previous = _redis.get(f"pwreset-user:{user_id}")
    if previous:
        _redis.delete(previous)
    _redis.set(_key(token), user_id, ex=settings.PASSWORD_RESET_TTL_SECONDS)
    _redis.set(f"pwreset-user:{user_id}", _key(token), ex=settings.PASSWORD_RESET_TTL_SECONDS)
    return token


def consume_reset_token(token: str) -> str | None:
    """Atomically read-and-delete: a token works exactly once."""
    user_id = _redis.getdel(_key(token))
    if user_id:
        _redis.delete(f"pwreset-user:{user_id}")
    return user_id
