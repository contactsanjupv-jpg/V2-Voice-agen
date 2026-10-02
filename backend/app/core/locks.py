"""
Tiny Redis mutex used to stop double-clicks / retries from running the same
provider-side operation twice at once. Not a distributed-systems framework:
one key, one owner, auto-expires so a crash can never wedge a customer.
"""
import uuid
from contextlib import contextmanager

import redis

from app.config import get_settings

_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class LockNotAcquired(Exception):
    pass


@contextmanager
def redis_lock(name: str, ttl_seconds: int = 60):
    key = f"lock:{name}"
    token = uuid.uuid4().hex
    if not _redis.set(key, token, nx=True, ex=ttl_seconds):
        raise LockNotAcquired(name)
    try:
        yield
    finally:
        _redis.eval(_RELEASE_SCRIPT, 1, key, token)