import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.db.base import get_db
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.db.models.telephony import PhoneNumber
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.call.retell_call_provider import RetellCallProvider
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellAPIError
from app.schemas.activation import ActivateRequest, ActivateResponse, TestCallResponse

router = APIRouter(prefix="/api/v1/orgs/{organization_id}", tags=["activation"])
settings = get_settings()


@router.post("/agents/{agent_id}/test-call", response_model=TestCallResponse)
def start_test_call(
    agent_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    """
    Backs the "Test AI" button (spec §16) — a browser WebRTC call against
    the draft agent config, no real phone number or telephony hop
    involved. Rate-limited per org to stop runaway Retell cost from
    repeated test calls (spec §27).
    """
    agent = (
        db.query(Agent)
        .filter(Agent.id == agent_id, Agent.organization_id == membership.organization_id)
        .first()
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    if agent.retell_agent_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Save the receptionist configuration before testing it")

    try:
        check_rate_limit(f"test-call:{membership.organization_id}", settings.TEST_CALLS_PER_DAY_PER_ORG, 86400)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily test call limit reached") from e

    provider = RetellCallProvider()
    try:
        session = provider.create_test_call(agent.retell_agent_id)
    except RetellAPIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not start a test call with the voice provider") from e

    if agent.status == AgentStatus.draft:
        agent.status = AgentStatus.testing
        db.commit()

    return TestCallResponse(call_id=session.provider_call_id, access_token=session.access_token)


@router.post("/activate", response_model=ActivateResponse)
def activate_receptionist(
    payload: ActivateRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.admin)),
    db: Session = Depends(get_db),
):
    """
    The final onboarding step (spec §16-17): binds a phone number to an
    agent and flips it live. Both resources are re-verified as belonging
    to THIS org before anything is touched — the IDs in the request body
    are never trusted on their own.
    """
    agent = (
        db.query(Agent)
        .filter(Agent.id == uuid.UUID(payload.agent_id), Agent.organization_id == membership.organization_id)
        .first()
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    phone_number = (
        db.query(PhoneNumber)
        .filter(
            PhoneNumber.id == uuid.UUID(payload.phone_number_id),
            PhoneNumber.organization_id == membership.organization_id,
        )
        .first()
    )
    if phone_number is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Phone number not found")
    if agent.retell_agent_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Receptionist isn't configured yet")

    provider = RetellPhoneProvider()
    try:
        provider.assign_agent(phone_number.retell_phone_number_id, agent.retell_agent_id, direction="inbound")
    except RetellAPIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not assign the number to the receptionist") from e

    phone_number.agent_id = agent.id
    agent.status = AgentStatus.active
    db.commit()

    return ActivateResponse(phone_number_id=str(phone_number.id), agent_id=str(agent.id), status="active")
