"""
The ONE authoritative definition of plans and what each plan includes.

* Plan <-> Paddle price mapping comes from config (PADDLE_*_PRICE_ID); a Paddle
  subscription's plan is always derived from its actual PRICE, never from
  client-influenced data.
* Features: Growth = Starter + GROWTH_ONLY. Inheritance is by construction, so
  Starter can never hold something Growth lacks.
* Every server-side feature gate goes through `require_feature` (auth/deps.py),
  which calls into here. Do not compare plan names anywhere else.

COMMERCIAL DECISIONS THAT ARE NOT MADE YET (do not invent them in code):
  - GROWTH_ONLY is empty: no feature that exists today has been designated
    Growth-only. Add a Feature member, list it in GROWTH_ONLY, and wrap the
    route in require_feature(Feature.x) — nothing else changes.
  - included_minutes is None (= not enforced) for both plans.
"""
import logging
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.billing_state import ACTIVE_STATUSES, current_subscription

logger = logging.getLogger("atla.plans")

UNKNOWN_PLAN = "unknown"


class Feature(str, Enum):
    phone_number = "phone_number"  # buy / keep a real business number (costs us money)
    go_live = "go_live"  # route real calls to the receptionist (costs us money)


STARTER_FEATURES: frozenset[Feature] = frozenset({Feature.phone_number, Feature.go_live})
GROWTH_ONLY_FEATURES: frozenset[Feature] = frozenset()


@dataclass(frozen=True)
class Plan:
    id: str
    rank: int
    features: frozenset[Feature]
    included_minutes: int | None = None  # None = not enforced (pricing decision pending)


PLANS: dict[str, Plan] = {
    "starter": Plan("starter", 1, STARTER_FEATURES),
    "growth": Plan("growth", 2, STARTER_FEATURES | GROWTH_ONLY_FEATURES),
}

_PRICE_SETTING = {"starter": "PADDLE_STARTER_PRICE_ID", "growth": "PADDLE_GROWTH_PRICE_ID"}


def price_id_for_plan(plan_id: str) -> str:
    name = _PRICE_SETTING.get(plan_id)
    return (getattr(get_settings(), name, "") or "") if name else ""


def plan_id_for_price(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for plan_id in _PRICE_SETTING:
        configured = price_id_for_plan(plan_id)
        if configured and configured == price_id:
            return plan_id
    return None


def plan_id_from_items(items) -> str | None:
    """
    Plan implied by a Paddle subscription's items.
      None        -> payload carried no price information at all
      "unknown"   -> prices present but not (only) ours: fail closed, no features
      "starter"/"growth" -> every price maps to that one plan
    """
    if not isinstance(items, list) or not items:
        return None
    resolved: set[str] = set()
    for item in items:
        price = item.get("price") if isinstance(item, dict) else None
        price_id = price.get("id") if isinstance(price, dict) else None
        if price_id:
            resolved.add(plan_id_for_price(price_id) or UNKNOWN_PLAN)
    if not resolved:
        return None
    return next(iter(resolved)) if len(resolved) == 1 else UNKNOWN_PLAN


def active_plan(db: Session, organization_id: uuid.UUID) -> Plan | None:
    """The plan the org is entitled to RIGHT NOW (trialing/active only). Unknown plan ids get nothing."""
    sub = current_subscription(db, organization_id)
    if sub is None or sub.status not in ACTIVE_STATUSES:
        return None
    plan = PLANS.get(sub.plan_id)
    if plan is None:
        logger.error("Active subscription %s has unrecognised plan_id %r — granting no features", sub.id, sub.plan_id)
    return plan


def plan_allows(db: Session, organization_id: uuid.UUID, feature: Feature) -> bool:
    plan = active_plan(db, organization_id)
    return plan is not None and feature in plan.features


def lowest_plan_with(feature: Feature) -> Plan | None:
    for plan in sorted(PLANS.values(), key=lambda p: p.rank):
        if feature in plan.features:
            return plan
    return None
