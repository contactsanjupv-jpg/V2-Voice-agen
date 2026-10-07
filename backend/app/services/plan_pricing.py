"""
The price shown to customers is the price of the Paddle price object behind the
plan (PADDLE_*_PRICE_ID) — read from Paddle, cached briefly in Redis. Nothing in
this codebase hardcodes an amount. If Paddle can't be read, the price is None and
the UI shows no number rather than a wrong one.
"""
import json
import logging

import redis

from app.config import get_settings
from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
from app.services.plans import price_id_for_plan

logger = logging.getLogger("atla.plan_pricing")

_redis = redis.from_url(get_settings().REDIS_URL, decode_responses=True)
_OK_TTL_SECONDS = 600
_FAIL_TTL_SECONDS = 30
CACHE_PREFIX = "plan-price:"


def _parse(data) -> dict | None:
    """Paddle price entity -> {amount_minor, currency, interval, interval_count}. None if not a usable recurring price."""
    if not isinstance(data, dict) or data.get("status") != "active":
        return None
    unit = data.get("unit_price") or {}
    amount, currency = unit.get("amount"), unit.get("currency_code")
    if not (isinstance(amount, (str, int)) and str(amount).isdigit() and currency):
        return None
    cycle = data.get("billing_cycle") or {}
    if not cycle.get("interval"):
        return None  # we only sell subscriptions
    return {
        "amount_minor": int(amount),
        "currency": currency,
        "interval": cycle["interval"],
        "interval_count": int(cycle.get("frequency") or 1),
    }


def price_for_plan(plan_id: str) -> dict | None:
    price_id = price_id_for_plan(plan_id)
    if not price_id:
        return None
    key = f"{CACHE_PREFIX}{price_id}"
    try:
        cached = _redis.get(key)
    except redis.RedisError:
        cached = None
    if cached is not None:
        return json.loads(cached)

    parsed, ttl = None, _FAIL_TTL_SECONDS
    try:
        parsed = _parse(PaddleBillingProvider().get_price(price_id))
        if parsed is not None:
            ttl = _OK_TTL_SECONDS
    except RuntimeError as e:  # includes PaddleAPIError and "not configured"
        logger.warning("Could not read price for plan %s from Paddle: %s", plan_id, e)
    try:
        _redis.set(key, json.dumps(parsed), ex=ttl)
    except redis.RedisError:
        pass
    return parsed