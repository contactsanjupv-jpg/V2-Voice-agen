"""
POST /checkout-session is the only way to start a purchase. The Subscription
row itself is written only by the Paddle webhook, never by this endpoint.
"""
import logging

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, get_current_user, require_role
from app.config import get_settings
from app.core.locks import LockNotAcquired, redis_lock
from app.services.billing_state import current_subscription, has_blocking_subscription
from app.services.plans import PLANS, price_id_for_plan
from app.db.base import get_db
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
from app.schemas.billing import CreateCheckoutSessionRequest, CreateCheckoutSessionResponse, SubscriptionOut

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/billing", tags=["billing"])
settings = get_settings()
logger = logging.getLogger("atla.billing")



@router.get("/subscription", response_model=SubscriptionOut | None)
def get_subscription(
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    subscription = current_subscription(db, membership.organization_id)
    return SubscriptionOut.model_validate(subscription) if subscription else None


@router.post("/checkout-session", response_model=CreateCheckoutSessionResponse)
def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if has_blocking_subscription(db, membership.organization_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "You already have a subscription. Manage it from your billing settings."
        )
    if payload.plan not in PLANS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown plan: {payload.plan}")
    price_id = price_id_for_plan(payload.plan)
    if not price_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable")

    try:
        # Lock so a double-click can't create two Paddle transactions.
        with redis_lock(f"checkout:{membership.organization_id}", ttl_seconds=30):
            provider = PaddleBillingProvider()
            session = provider.create_checkout_session(
                organization_id=str(membership.organization_id),
                plan_id=payload.plan,
                plan_price_id=price_id,
                customer_email=user.email,
                success_url=f"{settings.FRONTEND_URL}/dashboard/go-live",
                cancel_url=f"{settings.FRONTEND_URL}/dashboard/go-live",
            )
    except LockNotAcquired as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "Checkout is already starting — one moment.") from e
    except RuntimeError as e:  # includes PaddleAPIError
        logger.error("Checkout failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable") from e

    return CreateCheckoutSessionResponse(checkout_url=session.checkout_url)

class ManageOut(BaseModel):
    update_payment_method_url: str | None


def _paying_subscription(db: Session, organization_id):
    sub = current_subscription(db, organization_id)
    if sub is None or sub.status not in ("trialing", "active", "past_due", "paused") or not sub.external_subscription_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "There's no active subscription to manage.")
    return sub


@router.post("/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_subscription(
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    db: Session = Depends(get_db),
):
    """Cancel at the end of the paid period. Our state changes only when Paddle's webhook says so."""
    sub = _paying_subscription(db, membership.organization_id)
    if sub.cancel_effective_at is not None:
        return {"status": "already_scheduled"}
    try:
        PaddleBillingProvider().cancel_subscription(sub.external_subscription_id, immediately=False)
    except RuntimeError as e:
        logger.error("Cancel failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't cancel right now. Please try again.") from e
    return {"status": "requested"}


@router.get("/manage", response_model=ManageOut)
def manage_payment(
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    db: Session = Depends(get_db),
):
    sub = _paying_subscription(db, membership.organization_id)
    try:
        urls = PaddleBillingProvider().get_management_urls(sub.external_subscription_id)
    except RuntimeError as e:
        logger.error("Manage urls failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't load your billing details right now.") from e
    return ManageOut(update_payment_method_url=urls.get("update_payment_method"))
