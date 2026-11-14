"""
Inbound-call routing, two thin adapters over ONE policy (app/services/inbound_routing.py).

  RETELL     POST /webhooks/retell/inbound          Retell's `call_inbound` webhook for a real phone number.
                                                    Signature-verified; unauthenticated callers get a bare 401.
  SIMULATED  POST /api/v1/dev/simulate-inbound      Sandbox only (same guard as the unmetered test calls; 404 anywhere
                                                    else). Builds a Retell-shaped payload from fake numbers and an
                                                    optional pretend clock, then runs the SAME handler.

Both end in `handle_inbound_payload` -> `route_inbound_call` -> `render_response`. Neither contains hours or routing
logic, so switching from SIMULATED to RETELL changes where the payload comes from and nothing else.

Retell's inbound webhook carries no call-start time, so the instant is the moment we receive it (the call is ringing).
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.config import get_settings
from app.db.base import get_db
from app.db.models.tenancy import User
from app.services import monitoring
from app.services.inbound_routing import RouteDecision, route_inbound_call
from app.webhooks.retell_signature import InvalidRetellSignature, verify_retell_signature

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("atla.webhooks.retell_inbound")

_ERROR = RouteDecision("error", "closed", None, "bad_payload")


def _now() -> datetime:
    """The one clock both adapters read (tests replace it to pick the moment the 'call' arrives)."""
    return datetime.now(timezone.utc)


def render_response(decision: RouteDecision) -> dict:
    """Retell's call_inbound response. An agent id is present only when the policy chose one."""
    body: dict = {"metadata": {"atla_route": decision.status}}
    if decision.agent_id:
        body["override_agent_id"] = decision.agent_id
    return {"call_inbound": body}


def handle_inbound_payload(db: Session, payload: object, now: datetime) -> RouteDecision:
    """Shared by both adapters: payload (Retell shape) -> decision. A malformed payload is a closed decision."""
    try:
        to_number = payload["call_inbound"]["to_number"]  # type: ignore[index]
        if not isinstance(to_number, str):
            raise TypeError
    except (KeyError, TypeError):
        monitoring.count_event("inbound_route:bad_payload")
        return _ERROR
    decision = route_inbound_call(db, to_number=to_number, at=now)
    monitoring.count_event(f"inbound_route:{decision.status}")
    return decision


@router.post("/webhooks/retell/inbound")
async def retell_inbound(
    request: Request,
    db: Session = Depends(get_db),
    x_retell_signature: str | None = Header(default=None),
):
    raw_body = await request.body()
    try:
        verify_retell_signature(
            raw_body=raw_body,
            signature_header=x_retell_signature,
            api_key=settings.RETELL_API_KEY,
            max_skew_seconds=settings.RETELL_WEBHOOK_MAX_SKEW_SECONDS,
        )
    except InvalidRetellSignature as exc:
        logger.warning("Retell inbound webhook rejected: %s | body_bytes=%d", exc, len(raw_body))
        monitoring.count_event("webhook_rejected:retell_inbound")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        payload = json.loads(raw_body)
    except ValueError:
        payload = None
    decision = handle_inbound_payload(db, payload, _now())
    return JSONResponse(render_response(decision))


class SimulatedInbound(BaseModel):
    to_number: str = Field(min_length=1, max_length=32)  # a number of one of our (fake or sandbox) businesses
    from_number: str = Field(default="+15550100000", max_length=32)
    at: datetime | None = None  # pretend clock; naive = UTC. Omitted = now.


@router.post("/api/v1/dev/simulate-inbound")
def simulate_inbound(
    body: SimulatedInbound,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not settings.unmetered_test_calls_active:  # the existing local-sandbox guard, unmodified
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    at = body.at or _now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    payload = {
        "event": "call_inbound",
        "call_inbound": {"agent_id": "simulated", "from_number": body.from_number, "to_number": body.to_number},
    }
    decision = handle_inbound_payload(db, payload, at)
    return {"adapter": "simulated", "request": payload, "response": render_response(decision),
            "decision": {"status": decision.status, "target": decision.target, "reason": decision.reason}}
