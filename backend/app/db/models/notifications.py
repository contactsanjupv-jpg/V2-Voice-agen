import uuid

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class OwnerNotification(Base, UUIDPKMixin, TimestampMixin):
    """
    The EMAIL outbox (see services/owner_notifications.py). Email is reserved for password recovery and security
    alerts, which are sent by the account endpoints; NO operational kind is emailed, so nothing new is queued here
    (EMAIL_KINDS is empty). Rows left from before the policy are converted to in-app notifications and skipped.
    All operational alerts live in `Notification`.

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


CATEGORIES = ("calls", "leads", "credits", "billing", "receptionist", "phone", "business", "analytics", "product")
SEVERITIES = ("info", "warning", "critical")


class Notification(Base, UUIDPKMixin, TimestampMixin):
    """
    A durable IN-APP notification for ONE recipient (see services/notifications.py).

    One row per recipient, so read state is that person's own: `read_at` is NULL until THEY read it, and a colleague reading
    it changes nothing for them. Written in the SAME transaction as the event that caused it (the cause and its notification
    commit or roll back together).
    UNIQUE(organization_id, recipient_user_id, kind, dedupe_key) makes "tell them once" a database rule: a webhook
    redelivery, a sweeper retry or two racing workers cannot create a second row.
    `seq` is a database-assigned, increasing number for stable cursor paging and cheap "anything new?" checks.
    Title, body and link come from a fixed server-side catalogue; `payload` holds ids only (never transcripts).
    The link is a relative dashboard path, enforced by a CHECK so a bad value cannot be stored at all.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_notifications_seq"),
        UniqueConstraint("organization_id", "recipient_user_id", "kind", "dedupe_key", name="uq_notification_key"),
        Index("ix_notifications_recipient_seq", "recipient_user_id", "organization_id", "seq"),
        Index("ix_notifications_unread", "recipient_user_id", "organization_id", postgresql_where=text("read_at IS NULL")),
        CheckConstraint("category IN (" + ", ".join(f"'{c}'" for c in CATEGORIES) + ")", name="ck_notification_category"),
        CheckConstraint("severity IN (" + ", ".join(f"'{c}'" for c in SEVERITIES) + ")", name="ck_notification_severity"),
        CheckConstraint("link IS NULL OR link ~ '^/dashboard(/[A-Za-z0-9_-]+)*([?][A-Za-z0-9_=&.-]*)?(#[A-Za-z0-9_-]*)?$'", name="ck_notification_link"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info", server_default="info")
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    read_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationPreference(Base, UUIDPKMixin, TimestampMixin):
    """
    One person's in-app choice for one category in one organization (see services/notifications.py). No row means ON: nobody
    has to opt in. `enabled = false` stops NEW notifications of that category from being created for that person; critical
    notifications (payment failed, number could not be set up, ...) are always delivered. History is never touched.
    These are in-app preferences only: there is no email preference, because the only email is password recovery and security alerts.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", "category", name="uq_notification_preference"),
        CheckConstraint("category IN ('calls','leads','credits','billing','receptionist','phone','business','analytics','product')", name="ck_notification_preference_category"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
