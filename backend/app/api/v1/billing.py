"""
POST /checkout-session is the only way to get an org from "no subscription"
to "passes require_active_subscription" — everything downstream (the
Subscription row itself) is written by the Stripe webhook handler, never
by this endpoint directly, so a client can never fake "I paid."
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import current_membership, get_current_user, require_role
from app.config import get_settings
from app.db.base import get_db
from app.db.models.billing import Subscription
from app.db.models.tenancy import OrganizationMember, OrgRole, User
from app.providers.billing.stripe_billing_provider import StripeBillingProvider
from app.schemas.billing import CreateCheckoutSessionRequest, CreateCheckoutSessionResponse, SubscriptionOut

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/billing", tags=["billing"])
settings = get_settings()

_PLAN_PRICE_IDS = {
    "starter": lambda s: s.STRIPE_STARTER_PRICE_ID,
    "growth": lambda s: s.STRIPE_GROWTH_PRICE_ID,
}


@router.get("/subscription", response_model=SubscriptionOut | None)
def get_subscription(
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    subscription = (
        db.query(Subscription)
        .filter(Subscription.organization_id == membership.organization_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    return SubscriptionOut.model_validate(subscription) if subscription else None


@router.post("/checkout-session", response_model=CreateCheckoutSessionResponse)
def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    membership: OrganizationMember = Depends(require_role(OrgRole.owner)),
    user: User = Depends(get_current_user),
):
    price_id_getter = _PLAN_PRICE_IDS.get(payload.plan)
    if price_id_getter is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown plan: {payload.plan}")
    price_id = price_id_getter(settings)
    if not price_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable")

    try:
        provider = StripeBillingProvider()
        session = provider.create_checkout_session(
            organization_id=str(membership.organization_id),
            plan_id=payload.plan,
            plan_price_id=price_id,
            customer_email=user.email,
            success_url=f"{settings.FRONTEND_URL}/onboarding?billing=success",
            cancel_url=f"{settings.FRONTEND_URL}/onboarding?billing=cancelled",
        )
    except RuntimeError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Billing is temporarily unavailable") from e

    return CreateCheckoutSessionResponse(checkout_url=session.checkout_url)