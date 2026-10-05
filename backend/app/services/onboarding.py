"""
Where is this organization in onboarding? Derived ENTIRELY from what is already
saved (business, agent, calls) so a refresh, a closed browser or another device
resumes in the right place — and never re-imports the website or re-runs the LLM.
"""
import uuid

from sqlalchemy.orm import Session

from app.db.models.business import Business, BusinessStatus
from app.db.models.calls import Call, CallDirection
from app.db.models.voice_agent import Agent

MIN_TEST_SECONDS = 5  # a test call counts once it actually ran this long


def _info_for_review(business: Business) -> dict:
    """What the review step shows: the importer's own output if we have it, else the saved fields."""
    snapshot = business.raw_import_snapshot or {}
    if isinstance(snapshot.get("extracted"), dict):
        return snapshot["extracted"]
    return {
        "business_name": business.name,
        "description": business.description,
        "industry": business.industry,
        "services": [],
        "address": business.address,
        "phone": business.phone,
        "hours": business.hours,
        "faqs": [],
        "policies": [],
    }


def derive_onboarding(db: Session, organization_id: uuid.UUID) -> dict:
    business = (
        db.query(Business).filter(Business.organization_id == organization_id).order_by(Business.created_at.desc()).first()
    )
    state: dict = {"step": "website", "business_id": None, "business_name": None, "structured_info": None, "agent": None, "tested": False}
    if business is None:
        return state
    state.update(business_id=str(business.id), business_name=business.name)

    if business.status == BusinessStatus.draft:
        state.update(step="review", structured_info=_info_for_review(business))
        return state

    agent = db.query(Agent).filter(Agent.business_id == business.id, Agent.organization_id == organization_id).first()
    if agent is None:
        state["step"] = "voice"
        return state

    synced = agent.retell_agent_id is not None and agent.synced_version == agent.version
    state["agent"] = {"id": str(agent.id), "synced": synced, "voice_id": str(agent.voice_id) if agent.voice_id else None}
    if not synced:
        state["step"] = "behavior"
        return state

    tested = (
        db.query(Call.id)
        .filter(
            Call.organization_id == organization_id,
            Call.agent_id == agent.id,
            Call.direction == CallDirection.test,
            Call.status == "ended",
            Call.duration_seconds >= MIN_TEST_SECONDS,
        )
        .first()
        is not None
    )
    state.update(tested=tested, step="done" if tested else "test")
    return state
