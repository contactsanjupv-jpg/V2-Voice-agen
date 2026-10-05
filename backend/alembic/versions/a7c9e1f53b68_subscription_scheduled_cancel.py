"""subscription: scheduled-cancel date

Revision ID: a7c9e1f53b68
Revises: f6b8d0e42a57
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c9e1f53b68"
down_revision = "f6b8d0e42a57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("cancel_effective_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "cancel_effective_at")
