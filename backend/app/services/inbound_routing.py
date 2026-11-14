"""
Inbound routing policy: which receptionist answers a call. The ONE place that decides, whatever delivered the call.

Adapters (app/webhooks/retell_inbound.py) only translate their input into (called number, call start instant) and
translate the decision back into their output format. They contain no business rules.

Fail-closed invariant: the normal receptionist (the one with a transfer tool) is returned ONLY when the schedule is
enabled, fully valid, the business timezone is valid, and the evaluator says "open" at the call's start instant.
Closed, unknown, invalid schedule, invalid timezone, missing twin, database errors and any other exception all
resolve to the closed twin (or, if none exists yet, to "no override", i.e. the number's default agent, which an
enabled business has set to the twin).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.business import Business
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent
from app.services import monitoring
from app.services.hours_schedule import evaluate, is_enabled

logger = logging.getLogger("atla.inbound_routing")


@dataclass(frozen=True)
class RouteDecision:
    status: str  # open | closed | unknown | error | not_enabled | unknown_number
    target: str  # normal | closed | default
    agent_id: str | None  # provider agent id to answer with; None = no override (number's default agent answers)
    reason: str


def _closed(status: str, reason: str, agent: Agent | None = None) -> RouteDecision:
    twin = agent.closed_retell_agent_id if agent is not None else None
    return RouteDecision(status, "closed", twin, reason)


def routing_enabled(business: Business | None) -> bool:
    """Flag on AND this business explicitly opted in. Otherwise nothing about calls changes."""
    return bool(get_settings().AFTER_HOURS_ROUTING_ENABLED) and business is not None and is_enabled(business.hours_schedule)


def route_inbound_call(db: Session, *, to_number: str | None, at: datetime | None) -> RouteDecision:
    try:
        return _decide(db, to_number, at)
    except Exception:  # noqa: BLE001 - routing must never fail open
        logger.exception("Inbound routing failed; answering with the closed receptionist")
        monitoring.count_event("inbound_route:error")
        return RouteDecision("error", "closed", None, "routing_error")


def _decide(db: Session, to_number: str | None, at: datetime | None) -> RouteDecision:
    if not get_settings().AFTER_HOURS_ROUTING_ENABLED:
        return RouteDecision("not_enabled", "default", None, "flag_off")
    if not to_number:
        return RouteDecision("unknown_number", "default", None, "no_number")
    row = (
        db.query(PhoneNumber, Agent, Business)
        .join(Agent, PhoneNumber.agent_id == Agent.id)
        .join(Business, Agent.business_id == Business.id)
        .filter(PhoneNumber.number == to_number, PhoneNumber.status == PhoneNumberStatus.active)
        .first()
    )
    if row is None:
        return RouteDecision("unknown_number", "default", None, "number_not_routed")
    _number, agent, business = row
    if not is_enabled(business.hours_schedule):
        return RouteDecision("not_enabled", "default", None, "business_not_opted_in")
    status = evaluate(business.hours_schedule, business.timezone, at)
    if status == "closed":
        return _closed("closed", "outside_hours", agent)
    if status != "open":
        return _closed("unknown", "schedule_or_timezone_unusable", agent)
    if not agent.retell_agent_id or not agent.closed_retell_agent_id:
        # Open, but the twin that makes the number safe by default does not exist: refuse to open the transfer path.
        return _closed("unknown", "closed_twin_missing", agent)
    return RouteDecision("open", "normal", agent.retell_agent_id, "within_hours")
