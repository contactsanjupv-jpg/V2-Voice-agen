import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, has_active_subscription, require_feature, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.services.plans import Feature
from app.services.plans import Feature
from app.db.base import get_db
from app.db.models.calls import Call, CallDirection
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.call.retell_call_provider import RetellCallProvider
from app.services.activation import (
    ActivationBlocked,
    ActivationNotFound,
    ActivationUnverified,
    activate_receptionist as activate_service,
)
from app.providers.retell_client import RetellAPIError
from app.schemas.activation import ActivateRequest, ActivateResponse, TestCallResponse

router = APIRouter(prefix="/api/v1/orgs/{organization_id}", tags=["activation"])
settings = get_settings()
logger = logging.getLogger("atla.activation")


@router.post("/agents/{agent_id}/test-call", response_model=TestCallResponse)
def start_test_call(
    agent_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """
    Starts a real browser (WebRTC) call against the customer's saved agent.
    Every test call costs real Retell money, so this is gated four ways:
    the agent must be synced (the caller hears what was saved), a daily rate
    limit, a lifetime free allowance until the org has an active subscription,
    and a local Call row is created up front so the call is traceable.
    """
    agent = (
        db.query(Agent)
        .filter(Agent.id == agent_id, Agent.organization_id == membership.organization_id)
        .first()
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    if agent.retell_agent_id is None or agent.synced_version != agent.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your latest changes haven't been applied to your receptionist yet. Save it again, then test.",
        )

    if not has_active_subscription(db, membership.organization_id):
        used = (
            db.query(func.count(Call.id))
            .filter(Call.organization_id == membership.organization_id, Call.direction == CallDirection.test)
            .scalar()
        )
        if used >= settings.FREE_TEST_CALLS_PER_ORG:
            logger.warning(
                "Free test allowance exhausted [org=%s]: used=%s allowed=%s (no active subscription)",
                membership.organization_id, used, settings.FREE_TEST_CALLS_PER_ORG,
            )
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED, "You've used your free test calls. Choose a plan to keep testing."
            )

    try:
        check_rate_limit(f"test-call:{membership.organization_id}", settings.TEST_CALLS_PER_DAY_PER_ORG, 86400)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily test call limit reached") from e

    try:
        provider = RetellCallProvider()
        session = provider.create_test_call(
            agent.retell_agent_id,
            metadata={"organization_id": str(membership.organization_id), "agent_id": str(agent.id), "kind": "test"},
        )
    except RetellAPIError as e:
        # logger.exception records the full traceback INCLUDING the chained original
        # error (e.g. the httpx connect/timeout error behind status=0). Server-side only.
        logger.exception(
            "Retell test call failed [agent=%s]: status=%s ambiguous=%s body=%s error=%s",
            agent.id, e.status_code, e.ambiguous, e.body, e,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not start a test call with the voice provider") from e
    except RuntimeError as e:
        logger.exception("Test call provider unavailable [agent=%s]: %s", agent.id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Provider temporarily unavailable") from e

    # Local record up front: the Retell webhook for this call resolves against it.
    db.add(
        Call(
            organization_id=membership.organization_id,
            retell_call_id=session.provider_call_id,
            agent_id=agent.id,
            direction=CallDirection.test,
            status="registered",
        )
    )
    if agent.status == AgentStatus.draft:
        agent.status = AgentStatus.testing
    db.commit()

    return TestCallResponse(
        call_id=session.provider_call_id,
        access_token=session.access_token,
        max_seconds=settings.TEST_CALL_MAX_SECONDS,
    )


@router.post("/activate", response_model=ActivateResponse)
def activate_receptionist(
    payload: ActivateRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.admin)),
    _subscribed: OrganizationMember = Depends(require_feature(Feature.go_live)),
    db: Session = Depends(get_db),
):
    """
    Goes live ONLY when the provider confirms the number routes to this agent
    (services/activation.py). Ids in the body are re-verified against the org.
    Safe to call repeatedly.
    """
    try:
        agent_id, phone_id = uuid.UUID(payload.agent_id), uuid.UUID(payload.phone_number_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid id") from e
    try:
        agent = activate_service(db, membership.organization_id, agent_id, phone_id)
    except ActivationNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receptionist or phone number not found") from e
    except ActivationBlocked as e:
        raise HTTPException(status.HTTP_409_CONFLICT, e.reason) from e
    except ActivationUnverified as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "We couldn't confirm your receptionist is connected to your number yet. Please try again."
        ) from e
    except (RetellAPIError, RuntimeError) as e:
        logger.error("Activation provider error [org=%s]: %s", membership.organization_id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't reach our phone provider. Please try again.") from e
    return ActivateResponse(phone_number_id=str(phone_id), agent_id=str(agent.id), status=agent.status.value)
