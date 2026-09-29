import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, require_active_subscription, require_role
from app.auth.rate_limit import RateLimitExceeded, check_rate_limit
from app.config import get_settings
from app.db.base import get_db
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.tenancy import OrganizationMember, OrgRole
from app.providers.phone.retell_phone_provider import RetellPhoneProvider
from app.providers.retell_client import RetellAPIError
from app.schemas.catalog import PhoneNumberOut, PurchaseNumberRequest

logger = logging.getLogger("atla.phone_numbers")

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/phone-numbers", tags=["phone-numbers"])
settings = get_settings()


@router.get("", response_model=list[PhoneNumberOut])
def list_phone_numbers(
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    numbers = (
        db.query(PhoneNumber)
        .filter(PhoneNumber.organization_id == membership.organization_id)
        .order_by(PhoneNumber.created_at.desc())
        .all()
    )
    return [PhoneNumberOut(**{**n.__dict__, "id": str(n.id), "status": n.status.value}) for n in numbers]


@router.post("", response_model=PhoneNumberOut, status_code=status.HTTP_201_CREATED)
def purchase_phone_number(
    payload: PurchaseNumberRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.admin)),
    _subscribed: OrganizationMember = Depends(require_active_subscription),
    db: Session = Depends(get_db),
):
    """
    One call, one action: Retell doesn't have a browse-then-buy flow (see
    RetellPhoneProvider docstring) — this both requests and purchases a
    number in one step. area_code is a preference, not a guarantee.
    Costs real money, so it sits behind the subscription gate.
    """
    try:
        check_rate_limit(f"number-purchase:{membership.organization_id}", limit=10, window_seconds=86400)
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Daily phone number purchase limit reached") from e

    try:
        provider = RetellPhoneProvider()
        provisioned = provider.purchase_number(country=payload.country, area_code=payload.area_code)
    except RetellAPIError as e:
        logger.error(
            "Retell number provisioning failed [org=%s, country=%s, area_code=%s]: status=%s body=%s",
            membership.organization_id, payload.country, payload.area_code, e.status_code, e.body,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Number provisioning failed") from e
    except RuntimeError as e:
        logger.error("Phone provider unavailable: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Number provisioning is temporarily unavailable") from e

    number = PhoneNumber(
        organization_id=membership.organization_id,
        retell_phone_number_id=provisioned.provider_phone_number_id,
        number=provisioned.number,
        area_code=provisioned.area_code,
        country=provisioned.country,
        monthly_cost_cents=provisioned.monthly_cost_cents,
        status=PhoneNumberStatus.active,
    )
    db.add(number)
    db.commit()
    db.refresh(number)
    return PhoneNumberOut(**{**number.__dict__, "id": str(number.id), "status": number.status.value})


@router.delete("/{phone_number_id}", status_code=status.HTTP_204_NO_CONTENT)
def release_phone_number(
    phone_number_id: uuid.UUID,
    membership: OrganizationMember = Depends(require_role(OrgRole.admin)),
    db: Session = Depends(get_db),
):
    number = (
        db.query(PhoneNumber)
        .filter(PhoneNumber.id == phone_number_id, PhoneNumber.organization_id == membership.organization_id)
        .first()
    )
    if number is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Phone number not found")

    provider = RetellPhoneProvider()
    try:
        provider = RetellPhoneProvider()
        provider.release_number(number.retell_phone_number_id)
    except RetellAPIError as e:
        logger.error(
            "Retell number release failed [org=%s, phone_number_id=%s]: status=%s body=%s",
            membership.organization_id, phone_number_id, e.status_code, e.body,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not release number with provider") from e
    except RuntimeError as e:
        logger.error("Phone provider unavailable: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Provider temporarily unavailable") from e

    number.status = PhoneNumberStatus.released
    db.commit()