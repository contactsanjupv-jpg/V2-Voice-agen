"""in-app notifications: per-recipient durable notifications

Revision ID: b2d4f6a81c03
Revises: a1c3e5f70b92
Create Date: 2026-11-23

Adds `notifications` (one row per recipient; read state is per user). Nothing existing is altered, so rollback is a plain drop.
Chains after the Google sign-in revision (a1c3e5f70b92), keeping a single Alembic head.

The CHECK constraints are the last line of defence: the category and severity vocabularies are closed, and a deep link can only be
a relative /dashboard path (no scheme, no host, no "//"), whatever the application code does.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2d4f6a81c03"
down_revision = "a1c3e5f70b92"
branch_labels = None
depends_on = None

CATEGORIES = ("calls", "leads", "credits", "billing", "receptionist", "phone", "business", "analytics", "product")
SEVERITIES = ("info", "warning", "critical")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.String(500), nullable=False, server_default=""),
        sa.Column("link", sa.String(255), nullable=True),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("seq", name="uq_notifications_seq"),
        sa.UniqueConstraint("organization_id", "recipient_user_id", "kind", "dedupe_key", name="uq_notification_key"),
        sa.CheckConstraint("category IN (" + ", ".join(f"'{c}'" for c in CATEGORIES) + ")", name="ck_notification_category"),
        sa.CheckConstraint("severity IN (" + ", ".join(f"'{c}'" for c in SEVERITIES) + ")", name="ck_notification_severity"),
        sa.CheckConstraint("link IS NULL OR link ~ '^/dashboard(/[A-Za-z0-9_-]+)*([?][A-Za-z0-9_=&.-]*)?(#[A-Za-z0-9_-]*)?$'", name="ck_notification_link"),
    )
    op.create_index("ix_notifications_recipient_seq", "notifications", ["recipient_user_id", "organization_id", "seq"])
    op.create_index("ix_notifications_unread", "notifications", ["recipient_user_id", "organization_id"], postgresql_where=sa.text("read_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_notifications_unread", table_name="notifications")
    op.drop_index("ix_notifications_recipient_seq", table_name="notifications")
    op.drop_table("notifications")
