"""
POST /webhooks/stripe — the single ingress point for Stripe billing
events. This is what actually writes/updates `subscriptions` rows; the
checkout-session endpoint never writes billing state directly.
"""
import logging

import stripe
from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models.billing import Subscription
from app.db.models.platform import WebhookEvent, WebhookSource

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("atla.webhooks.stripe")


@router.post("/webhooks/stripe", status_code=status.HTTP_204_NO_CONTENT)
async def handle_stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    raw_body = await request.body()

    try:
        event = stripe.Webhook.construct_event(raw_body, stripe_signature, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    event = event.to_dict()

    webhook_event = WebhookEvent(
        source=WebhookSource.stripe,
        external_event_id=event["id"],
        event_type=event["type"],
        payload=event,
    )
    db.add(webhook_event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    _process_event(db, event)

    from datetime import datetime, timezone

    webhook_event.processed_at = datetime.now(timezone.utc)
    webhook_event.status = "processed"
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _process_event(db: Session, event: dict) -> None:
    event_type = event["type"]
    data_object = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _upsert_subscription_from_checkout(db, data_object)
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        _sync_subscription_status(db, data_object, canceled=(event_type == "customer.subscription.deleted"))
    else:
        logger.info("Unhandled Stripe event type: %s", event_type)


def _upsert_subscription_from_checkout(db: Session, session: dict) -> None:
    metadata = session.get("metadata") or {}
    organization_id = metadata.get("organization_id") or session.get("client_reference_id")
    plan_id = metadata.get("plan_id", "unknown")
    external_subscription_id = session.get("subscription")

    if not organization_id or not external_subscription_id:
        logger.error("checkout.session.completed missing organization_id or subscription id: %s", session.get("id"))
        return

    import uuid as uuid_module

    subscription = (
        db.query(Subscription)
        .filter(Subscription.organization_id == uuid_module.UUID(organization_id))
        .first()
    )
    if subscription is None:
        subscription = Subscription(organization_id=uuid_module.UUID(organization_id))
        db.add(subscription)

    subscription.billing_provider = "stripe"
    subscription.external_subscription_id = external_subscription_id
    subscription.plan_id = plan_id
    subscription.status = "active"
    db.commit()


def _sync_subscription_status(db: Session, stripe_subscription: dict, canceled: bool) -> None:
    external_subscription_id = stripe_subscription.get("id")
    subscription = (
        db.query(Subscription)
        .filter(Subscription.external_subscription_id == external_subscription_id)
        .first()
    )
    if subscription is None:
        logger.warning("Received subscription event for unknown subscription: %s", external_subscription_id)
        return

    subscription.status = "canceled" if canceled else stripe_subscription.get("status", subscription.status)
    db.commit()