from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import current_membership
from app.db.base import get_db
from app.db.models.tenancy import OrganizationMember
from app.services.usage import usage_summary

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/usage", tags=["usage"])


class UsageOut(BaseModel):
    period_start: datetime | None
    period_end: datetime | None
    calls_count: int
    billable_seconds: int


@router.get("", response_model=UsageOut)
def get_usage(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    """Only what the customer should see: calls and seconds. No provider cost."""
    return usage_summary(db, membership.organization_id)
