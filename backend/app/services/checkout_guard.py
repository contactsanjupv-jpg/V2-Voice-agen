"""
One open checkout per organization.

Why: a Paddle transaction exists (and can be paid) from the moment we create it.
If a customer opens checkout twice and pays both, Paddle creates two subscriptions
and charges twice. So while a checkout is pending we hand back the SAME checkout
URL for the same plan, and refuse to open a second one for a different plan.
The marker clears when the subscription webhook arrives or after the TTL.
(services/duplicate_subscriptions.py is the second line of defence.)
"""
import json

import redis

from app.config import get_settings

_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
PENDING_TTL_SECONDS = 900


def _key(organization_id) -> str:
    return f"pending-checkout:{organization_id}"


def get_pending(organization_id) -> dict | None:
    raw = _redis.get(_key(organization_id))
    return json.loads(raw) if raw else None


def set_pending(organization_id, plan_id: str, checkout_url: str) -> None:
    _redis.set(_key(organization_id), json.dumps({"plan": plan_id, "checkout_url": checkout_url}), ex=PENDING_TTL_SECONDS)


def clear_pending(organization_id) -> None:
    _redis.delete(_key(organization_id))