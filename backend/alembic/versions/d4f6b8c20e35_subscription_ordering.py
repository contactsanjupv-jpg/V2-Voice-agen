"""subscriptions: event ordering columns + one row per provider subscription

Revision ID: d4f6b8c20e35
Revises: c3e5a7b91d24
"""
import sqlalchemy as sa
from alembic import op

revision = "d4f6b8c20e35"
down_revision = "c3e5a7b91d24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_subscriptions_external",
        "subscriptions",
        ["billing_provider", "external_subscription_id"],
        unique=True,
        postgresql_where=sa.text("external_subscription_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_subscriptions_external", table_name="subscriptions")
    op.drop_column("subscriptions", "status_changed_at")
    op.drop_column("subscriptions", "last_event_at")
