import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class WebhookSource(str, enum.Enum):
    retell = "retell"
    calendar_oauth = "calendar_oauth"
    paddle = "paddle"


class WebhookEvent(Base, UUIDPKMixin, TimestampMixin):
    """
    The idempotency guard for duplicate webhook deliveries. Retell retries
    up to 3x on a non-2xx / timeout — UNIQUE(source, external_event_id)
    means a duplicate delivery is a harmless no-op instead of a duplicate
    lead/appointment/billing event.
    """
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("source", "external_event_id", name="uq_webhook_source_event"),)

    source: Mapped[WebhookSource] = mapped_column(Enum(WebhookSource, name="webhook_source"), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # received -> processed | failed (retryable, backoff) -> dead (gave up; visible, re-queueable)
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    audit_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AdminUser(Base, UUIDPKMixin, TimestampMixin):
    """
    Deliberately separate from organization_members. Platform admin access
    is global, not scoped to a tenant, and is never granted merely by being
    an 'owner' of some organization.
    """
    __tablename__ = "admin_users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
