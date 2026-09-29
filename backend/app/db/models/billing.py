import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class Subscription(Base, UUIDPKMixin, TimestampMixin):
    """
    Deliberately provider-agnostic: `billing_provider` + `external_subscription_id`
    instead of Stripe-specific columns, so swapping billing providers later
    doesn't require a schema change. See app/providers/billing (Phase 7).
    """
    __tablename__ = "subscriptions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    billing_provider: Mapped[str] = mapped_column(String(32), default="paddle", nullable=False)
    external_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="trialing", nullable=False)
    current_period_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageRecord(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "usage_records"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    minutes_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    calls_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retell_cost_cents_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
