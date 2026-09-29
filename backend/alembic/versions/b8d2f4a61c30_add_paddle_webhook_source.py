"""add paddle webhook source

Revision ID: b8d2f4a61c30
Revises: 7c81a009aa17
"""
from alembic import op

revision = "b8d2f4a61c30"
down_revision = "7c81a009aa17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE webhook_source ADD VALUE IF NOT EXISTS 'paddle'")


def downgrade() -> None:
    pass  # Postgres cannot drop enum values