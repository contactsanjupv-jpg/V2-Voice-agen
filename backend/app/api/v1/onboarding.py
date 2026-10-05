from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import current_membership
from app.db.base import get_db
from app.db.models.tenancy import OrganizationMember
from app.services.onboarding import derive_onboarding

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/onboarding", tags=["onboarding"])


class OnboardingAgent(BaseModel):
    id: str
    synced: bool
    voice_id: str | None


class OnboardingOut(BaseModel):
    step: str  # website | review | voice | behavior | test | done
    business_id: str | None
    business_name: str | None
    structured_info: dict | None  # present only on the review step
    agent: OnboardingAgent | None
    tested: bool


@router.get("", response_model=OnboardingOut)
def get_onboarding(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    return derive_onboarding(db, membership.organization_id)
