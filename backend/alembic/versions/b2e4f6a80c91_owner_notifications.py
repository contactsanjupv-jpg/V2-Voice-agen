"""owner notification outbox

Revision ID: b2e4f6a80c91
Revises: b1d3f5a74e96
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2e4f6a80c91"
down_revision = "b1d3f5a74e96"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "kind", "dedupe_key", name="uq_owner_notification_key"),
    )
    op.create_index("ix_owner_notifications_organization_id", "owner_notifications", ["organization_id"])
    op.create_index("ix_owner_notifications_due", "owner_notifications", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_owner_notifications_due", table_name="owner_notifications")
    op.drop_index("ix_owner_notifications_organization_id", table_name="owner_notifications")
    op.drop_table("owner_notifications")
