"""notification preferences: per-user, per-category in-app mutes

Revision ID: c3e5a7b92d14
Revises: b2d4f6a81c03
Create Date: 2026-11-26

Adds `notification_preferences` (no row = on). Nothing existing is altered, so rollback is a plain drop.
Chains after the in-app notifications revision (b2d4f6a81c03), keeping a single Alembic head.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3e5a7b92d14"
down_revision = "b2d4f6a81c03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.UniqueConstraint("organization_id", "user_id", "category", name="uq_notification_preference"),
        sa.CheckConstraint(
            "category IN ('calls','leads','credits','billing','receptionist','phone','business','analytics','product')",
            name="ck_notification_preference_category",
        ),
    )
    op.create_index("ix_notification_preferences_organization_id", "notification_preferences", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_preferences_organization_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")
