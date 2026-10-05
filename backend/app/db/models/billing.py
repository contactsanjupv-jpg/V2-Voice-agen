import uuid
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    __table_args__ = (
        Index(
            "uq_subscriptions_external",
            "billing_provider",
            "external_subscription_id",
            unique=True,
            postgresql_where="external_subscription_id IS NOT NULL",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    billing_provider: Mapped[str] = mapped_column(String(32), default="paddle", nullable=False)
    external_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="trialing", nullable=False)
    current_period_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `occurred_at` of the newest provider event applied. Older events are
    # ignored (webhooks are at-least-once and may arrive out of order).
    last_event_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the customer cancelled and service continues until this date.
    cancel_effective_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When `status` last CHANGED (provider time) — drives grace-period policy.
    status_changed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageLedgerEntry(Base, UUIDPKMixin, TimestampMixin):
    """
    One row per finished call: the auditable record of what was used and what
    the provider says it cost us. Unique on retell_call_id, so a duplicate
    webhook can never double-count. Customer usage and our margin are both
    SUMs over this table — there is no separate counter that can drift.
    """

    __tablename__ = "usage_ledger"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    retell_call_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "production" | "test"
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    billable_seconds: Mapped[int] = mapped_column(Integer, nullable=False)  # duration rounded to the nearest second
    # Retell's reported call_cost.combined_cost, stored as received. UNIT UNVERIFIED
    # (believed to be cents) — confirm against one real call before using for margin.
    provider_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    provider_cost_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    period_start: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
