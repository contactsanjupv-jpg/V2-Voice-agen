"""Single source of truth for "what is this org's billing state". Every gate,
endpoint and job reads subscriptions through here so the rules can't drift."""
import uuid

from sqlalchemy.orm import Session

from app.db.models.billing import Subscription

ACTIVE_STATUSES = ("trialing", "active")
# A new checkout is refused while the org has a subscription in any of these
# states — past_due/paused customers must fix the existing subscription, not buy a second one.
BLOCKS_NEW_CHECKOUT = ("trialing", "active", "past_due", "paused")


def _org_subscriptions(db: Session, organization_id: uuid.UUID):
    return db.query(Subscription).filter(Subscription.organization_id == organization_id)


def has_active_subscription(db: Session, organization_id: uuid.UUID) -> bool:
    return _org_subscriptions(db, organization_id).filter(Subscription.status.in_(ACTIVE_STATUSES)).first() is not None


def has_blocking_subscription(db: Session, organization_id: uuid.UUID) -> bool:
    return _org_subscriptions(db, organization_id).filter(Subscription.status.in_(BLOCKS_NEW_CHECKOUT)).first() is not None


def current_subscription(db: Session, organization_id: uuid.UUID) -> Subscription | None:
    """The paying subscription if there is one, otherwise the most recent row."""
    q = _org_subscriptions(db, organization_id)
    active = q.filter(Subscription.status.in_(ACTIVE_STATUSES)).order_by(Subscription.created_at.desc()).first()
    return active or q.order_by(Subscription.created_at.desc()).first()
