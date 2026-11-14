import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class OwnerNotification(Base, UUIDPKMixin, TimestampMixin):
    """
    A durable "tell the business owner" outbox (see services/owner_notifications.py).

    A row is written in the SAME transaction as the event that causes it (a lead captured, service paused, a payment
    that failed), so the notification exists if and only if the cause was committed. The worker sends it afterwards
    and retries failures. UNIQUE(organization_id, kind, dedupe_key) makes "tell them once" a database rule, not a
    hope. `payload` carries ids only, never customer text: the message is built from live data when it is sent.
    """

    __tablename__ = "owner_notifications"
    __table_args__ = (
        UniqueConstraint("organization_id", "kind", "dedupe_key", name="uq_owner_notification_key"),
        Index("ix_owner_notifications_due", "status", "next_attempt_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # pending (new, or waiting to retry) -> sent | skipped (no longer relevant / nobody to tell) | failed (gave up)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
