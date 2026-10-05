"""Customer-facing usage and internal margin inputs, both derived from the ledger."""
import uuid
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.billing import UsageLedgerEntry
from app.services.billing_state import ACTIVE_STATUSES, current_subscription


def usage_summary(db: Session, organization_id: uuid.UUID) -> dict:
    """Production calls in the current billing period (all-time if there is none)."""
    sub = current_subscription(db, organization_id)
    period_start = sub.current_period_start if sub and sub.status in ACTIVE_STATUSES else None
    period_end = sub.current_period_end if sub and sub.status in ACTIVE_STATUSES else None
    q = db.query(func.count(UsageLedgerEntry.id), func.coalesce(func.sum(UsageLedgerEntry.billable_seconds), 0)).filter(
        UsageLedgerEntry.organization_id == organization_id, UsageLedgerEntry.kind == "production"
    )
    if period_start and period_end:
        q = q.filter(UsageLedgerEntry.occurred_at >= period_start, UsageLedgerEntry.occurred_at < period_end)
    calls, seconds = q.one()
    return {"period_start": period_start, "period_end": period_end, "calls_count": int(calls), "billable_seconds": int(seconds)}


def provider_cost_total(db: Session, organization_id: uuid.UUID, kind: str | None = None) -> Decimal:
    """INTERNAL ONLY (unit UNVERIFIED, see UsageLedgerEntry): never expose to customers."""
    q = db.query(func.coalesce(func.sum(UsageLedgerEntry.provider_cost), 0)).filter(
        UsageLedgerEntry.organization_id == organization_id
    )
    if kind:
        q = q.filter(UsageLedgerEntry.kind == kind)
    return Decimal(q.scalar())
