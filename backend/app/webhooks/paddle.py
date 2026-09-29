"""
POST /webhooks/paddle — the only writer of `subscriptions` rows. The
checkout endpoint never writes billing state, so a client can't fake "I paid".
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models.billing import Subscription
from app.db.models.platform import WebhookEvent, WebhookSource
from app.db.models.tenancy import Organization

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("atla.webhooks.paddle")

_SUBSCRIPTION_EVENTS = {
    "subscription.created",
    "subscription.activated",
    "subscription.updated",
    "subscription.canceled",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
}
_ACTIVE = ("trialing", "active")


def verify_paddle_signature(
    raw_body: bytes, header: str | None, secret: str, max_skew_seconds: int, now: float | None = None
) -> bool:
    """Header format: ts=<unix>;h1=<hex> (several h1 during secret rotation).
    Signed payload is b'<ts>:' + raw body, HMAC-SHA256 with the endpoint secret."""
    if not header or not secret:
        return False
    ts = None
    signatures: list[str] = []
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            ts = value
        elif key == "h1":
            signatures.append(value)
    if not ts or not signatures:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts_int) > max_skew_seconds:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b":" + raw_body, hashlib.sha256).hexdigest().encode()
    return any(hmac.compare_digest(sig.encode(), expected) for sig in signatures)


@router.post("/webhooks/paddle", status_code=status.HTTP_204_NO_CONTENT)
async def handle_paddle_webhook(
    request: Request,
    db: Session = Depends(get_db),
    paddle_signature: str | None = Header(default=None, alias="Paddle-Signature"),
):
    raw_body = await request.body()

    if not verify_paddle_signature(
        raw_body, paddle_signature, settings.PADDLE_WEBHOOK_SECRET, settings.PADDLE_WEBHOOK_MAX_SKEW_SECONDS
    ):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        event = json.loads(raw_body)
        event_id = event["event_id"]
        event_type = event["event_type"]
        data = event["data"]
        if not isinstance(data, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    record = WebhookEvent(
        source=WebhookSource.paddle, external_event_id=event_id, event_type=event_type, payload=event
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # duplicate delivery — already handled
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Record + process in ONE transaction: if processing fails, the event row
    # rolls back too, so Paddle's retry is processed instead of dropped as a duplicate.
    try:
        if event_type in _SUBSCRIPTION_EVENTS:
            _sync_subscription(db, data)
        else:
            logger.info("Unhandled Paddle event type: %s", event_type)
        record.processed_at = datetime.now(timezone.utc)
        record.status = "processed"
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Paddle webhook processing failed: %s", event_id)
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _sync_subscription(db: Session, data: dict) -> None:
    external_id = data.get("id")
    new_status = data.get("status")
    custom = data.get("custom_data") or {}
    if not external_id or not new_status:
        logger.error("Paddle subscription event missing id/status")
        return

    subscription = (
        db.query(Subscription).filter(Subscription.external_subscription_id == external_id).first()
    )
    if subscription is None:
        org_id = _parse_uuid(custom.get("organization_id"))
        if org_id is None or db.get(Organization, org_id) is None:
            logger.error("Paddle subscription %s has no valid organization_id in custom_data", external_id)
            return
        subscription = (
            db.query(Subscription)
            .filter(Subscription.organization_id == org_id)
            .order_by(Subscription.created_at.desc())
            .first()
        )
        if subscription is None:
            subscription = Subscription(organization_id=org_id, plan_id=custom.get("plan_id") or "unknown")
            db.add(subscription)
        elif subscription.external_subscription_id and subscription.status in _ACTIVE and new_status not in _ACTIVE:
            # Late event for an OLD subscription must not downgrade the org's current one.
            logger.warning("Ignoring stale event for old subscription %s", external_id)
            return

    subscription.billing_provider = "paddle"
    subscription.external_subscription_id = external_id
    subscription.status = new_status
    if custom.get("plan_id"):
        subscription.plan_id = custom["plan_id"]
    period = data.get("current_billing_period") or {}
    subscription.current_period_start = _parse_dt(period.get("starts_at")) or subscription.current_period_start
    subscription.current_period_end = _parse_dt(period.get("ends_at")) or subscription.current_period_end