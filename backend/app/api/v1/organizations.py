from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db.base import get_db
from app.db.models.tenancy import Organization, OrganizationMember, User
from app.schemas.auth import OrganizationOut

router = APIRouter(prefix="/api/v1/orgs", tags=["organizations"])


@router.get("/me", response_model=list[OrganizationOut])
def list_my_organizations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Every org the current user belongs to, with their role in each —
    this is how the frontend learns which organization_id to use for
    every other endpoint (never guessed, never hardcoded).
    """
    rows = (
        db.query(Organization, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .filter(OrganizationMember.user_id == user.id)
        .all()
    )
    return [OrganizationOut(id=str(org.id), name=org.name, role=member.role.value) for org, member in rows]
