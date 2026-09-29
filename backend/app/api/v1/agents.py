import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_role
from app.config import get_settings
from app.db.base import get_db
from app.db.models.business import Business
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.db.models.voice_agent import Agent, Voice
from app.providers.retell_client import RetellAPIError
from app.schemas.catalog import AgentConfigRequest, AgentOut
from app.services.agent_service import create_or_update_agent

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/agents", tags=["agents"])
settings = get_settings()


@router.get("", response_model=list[AgentOut])
def list_agents(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    agents = db.query(Agent).filter(Agent.organization_id == membership.organization_id).all()
    return [AgentOut(id=str(a.id), name=a.name, status=a.status.value, retell_agent_id=a.retell_agent_id) for a in agents]


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

    voice = db.query(Voice).filter(Voice.id == uuid.UUID(payload.voice_id)).first()
    if voice is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown voice_id")

    existing_agent = db.query(Agent).filter(Agent.business_id == business.id).first()
    if existing_agent is not None:
        existing_agent.name = payload.name
        existing_agent.greeting = payload.greeting
        existing_agent.personality = payload.personality
        existing_agent.language = payload.language
        existing_agent.tasks = payload.tasks
        existing_agent.transfer_number = payload.transfer_number
        existing_agent.business_hours = payload.business_hours
        existing_agent.after_hours_behavior = payload.after_hours_behavior

    try:
        agent = create_or_update_agent(
            db,
            organization_id=membership.organization_id,
            business=business,
            agent=existing_agent,
            voice=voice,
            webhook_base_url=settings.FRONTEND_URL.replace("3000", "8000"),  # backend's own public URL in real deploy
        )
    except RetellAPIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not save receptionist configuration with the voice provider") from e

    return AgentOut(id=str(agent.id), name=agent.name, status=agent.status.value, retell_agent_id=agent.retell_agent_id)
