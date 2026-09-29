"""
Fixed-window rate limiting on Redis. Deliberately simple (INCR + EXPIRE) —
good enough for login/signup/import/test-call throttling; swap for a
sliding-window/token-bucket implementation only if abuse patterns show the
fixed-window edge effect actually matters in practice.
"""
import redis

from app.config import get_settings

settings = get_settings()
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded, retry after {retry_after_seconds}s")


def check_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """Raises RateLimitExceeded if `key` has been hit more than `limit`
    times in the current window. Call BEFORE doing the expensive/sensitive
    work, not after."""
    full_key = f"ratelimit:{key}"
    current = _redis.incr(full_key)
    if current == 1:
        _redis.expire(full_key, window_seconds)
    if current > limit:
        ttl = _redis.ttl(full_key)
        raise RateLimitExceeded(retry_after_seconds=max(ttl, 1))
