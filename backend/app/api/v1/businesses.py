import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.db.base import get_db
from app.db.models.business import Business, BusinessStatus, KnowledgeItem, KnowledgeItemSource, KnowledgeItemType
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.schemas.business import (
    BusinessApproveRequest,
    BusinessReviewOut,
    CreateManualBusinessRequest,
    ImportWebsiteRequest,
    ImportWebsiteResponse,
)
from app.services.website_import_service import WebsiteImportError, import_website

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/businesses", tags=["businesses"])
settings = get_settings()


@router.post("/import-website", response_model=ImportWebsiteResponse, status_code=status.HTTP_201_CREATED)
def import_website_endpoint(
    payload: ImportWebsiteRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    try:
        check_rate_limit(
            f"website-import:{membership.organization_id}",
            settings.WEBSITE_IMPORTS_PER_DAY_PER_ORG,
            86400,
        )
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily website import limit reached") from e

    try:
        result = import_website(str(payload.url))
    except WebsiteImportError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    info = result.structured_info
    business = Business(
        organization_id=membership.organization_id,
        name=info.get("business_name") or "New business",
        website_url=str(payload.url),
        industry=info.get("industry"),
        address=info.get("address"),
        description=info.get("description"),
        hours=info.get("hours"),
        phone=info.get("phone"),
        status=BusinessStatus.draft,
        raw_import_snapshot={**result.raw_snapshot, "extracted": info},
    )
    db.add(business)
    db.commit()
    db.refresh(business)

    return ImportWebsiteResponse(
        business_id=str(business.id),
        pages_fetched=result.pages_fetched,
        structured_info=info,
    )


@router.post("/create-manual", response_model=BusinessReviewOut, status_code=status.HTTP_201_CREATED)
def create_manual_business(
    payload: CreateManualBusinessRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    """
    The "I don't have a website" path — skips the importer entirely and
    creates a business record the customer fills in by hand on the same
    review/approve screen the import flow uses. No rate limit needed here
    (unlike import-website): this is a single local DB insert, not an
    outbound fetch or an LLM call, so there's no external cost or SSRF
    surface to protect against.
    """
    business = Business(
        organization_id=membership.organization_id,
        name=payload.name.strip() or "New business",
        website_url=None,
        status=BusinessStatus.draft,
        raw_import_snapshot=None,
    )
    db.add(business)
    db.commit()
    db.refresh(business)
    return BusinessReviewOut(**{**business.__dict__, "id": str(business.id), "status": business.status.value})


@router.get("/{business_id}", response_model=BusinessReviewOut)
def get_business_for_review(
    business_id: uuid.UUID,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    return BusinessReviewOut(**{**business.__dict__, "id": str(business.id), "status": business.status.value})


@router.post("/{business_id}/approve", response_model=BusinessReviewOut)
def approve_business(
    business_id: uuid.UUID,
    payload: BusinessApproveRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.member)),
    db: Session = Depends(get_db),
):
    """
    Spec §7: "The customer must approve/edit the information before it
    becomes authoritative business knowledge." This is that moment —
    everything the customer confirmed (edited or not) is written into
    `businesses` + `knowledge_items` here; nothing from the raw import is
    trusted as knowledge before this call.
    """
    business = (
        db.query(Business)
        .filter(Business.id == business_id, Business.organization_id == membership.organization_id)
        .first()
    )
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")

    business.name = payload.name
    business.industry = payload.industry
    business.address = payload.address
    business.description = payload.description
    business.hours = payload.hours
    business.phone = payload.phone
    business.status = BusinessStatus.reviewed
    business.approved_at = datetime.now(timezone.utc)

    db.query(KnowledgeItem).filter(
        KnowledgeItem.business_id == business.id, KnowledgeItem.source == KnowledgeItemSource.import_
    ).delete()

    for service in payload.services:
        db.add(
            KnowledgeItem(
                business_id=business.id,
                type=KnowledgeItemType.service,
                title=service[:255],
                content=service,
                source=KnowledgeItemSource.import_,
            )
        )
    for faq in payload.faqs:
        q, a = faq.get("question", ""), faq.get("answer", "")
        db.add(
            KnowledgeItem(
                business_id=business.id,
                type=KnowledgeItemType.faq,
                title=q[:255] or "FAQ",
                content=f"Q: {q}\nA: {a}",
                source=KnowledgeItemSource.import_,
            )
        )
    for policy in payload.policies:
        db.add(
            KnowledgeItem(
                business_id=business.id,
                type=KnowledgeItemType.policy,
                title=policy[:255],
                content=policy,
                source=KnowledgeItemSource.import_,
            )
        )

    db.commit()
    db.refresh(business)
    return BusinessReviewOut(**{**business.__dict__, "id": str(business.id), "status": business.status.value})