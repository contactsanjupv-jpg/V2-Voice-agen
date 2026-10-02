import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_role
from app.core.locks import LockNotAcquired
from app.db.base import get_db
from app.db.models.business import Business
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.db.models.voice_agent import Agent, Voice
from app.providers.retell_client import RetellAPIError
from app.schemas.catalog import AgentConfigRequest, AgentOut
from app.services.agent_service import save_agent_settings, sync_agent_to_provider

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/agents", tags=["agents"])
logger = logging.getLogger("atla.agents")


def _agent_out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=str(agent.id),
        name=agent.name,
        status=agent.status.value,
        retell_agent_id=agent.retell_agent_id,
        synced=agent.retell_agent_id is not None and agent.synced_version == agent.version,
    )


@router.get("", response_model=list[AgentOut])
def list_agents(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    agents = db.query(Agent).filter(Agent.organization_id == membership.organization_id).all()
    return [_agent_out(a) for a in agents]


@router.put("/{business_id}", response_model=AgentOut)
def upsert_agent(
    business_id: uuid.UUID,
    payload: AgentConfigRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    try:
        voice = db.query(Voice).filter(Voice.id == uuid.UUID(payload.voice_id)).first()
    except ValueError:
        voice = None
    if voice is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown voice_id")

    agent = save_agent_settings(
        db, organization_id=membership.organization_id, business=business, voice=voice, payload=payload
    )

    try:
        agent = sync_agent_to_provider(db, agent, business, voice)
    except LockNotAcquired as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "A save is already in progress — try again in a moment") from e
    except RetellAPIError as e:
        logger.error(
            "Retell sync failed [agent=%s]: status=%s ambiguous=%s body=%s", agent.id, e.status_code, e.ambiguous, e.body
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Your changes are saved, but we couldn't apply them to your receptionist yet. Please try again.",
        ) from e
    except RuntimeError as e:
        logger.error("Voice provider not configured: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Voice provider temporarily unavailable") from e

    return _agent_out(agent)