import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.auth.deps import current_membership, require_role
from app.db.base import get_db
from app.db.models.crm import Lead, LeadStatus
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.schemas.calls_leads import LeadOut

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/leads", tags=["leads"])

MAX_PAGE_SIZE = 100


class LeadStatusUpdate(BaseModel):
    status: str


@router.get("", response_model=list[LeadOut])
def list_leads(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=25, le=MAX_PAGE_SIZE, gt=0),
    offset: int = Query(default=0, ge=0),
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    query = db.query(Lead).filter(Lead.organization_id == membership.organization_id)
    if status_filter:
        try:
            query = query.filter(Lead.status == LeadStatus(status_filter))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status: {status_filter}")
    leads = query.order_by(Lead.created_at.desc()).limit(limit).offset(offset).all()
    return [LeadOut.model_validate(lead) for lead in leads]


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead_status(
    lead_id: uuid.UUID,
    payload: LeadStatusUpdate,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.organization_id == membership.organization_id).first()
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")

    try:
        lead.status = LeadStatus(payload.status)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status: {payload.status}")

    db.commit()
    db.refresh(lead)
    return LeadOut.model_validate(lead)