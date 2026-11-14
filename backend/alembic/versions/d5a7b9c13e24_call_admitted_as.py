"""calls.admitted_as: how a test call was admitted (durable usage classification)

Revision ID: d5a7b9c13e24
Revises: c4f6a8b02d13
"""
import sqlalchemy as sa
from alembic import op

revision = "d5a7b9c13e24"
down_revision = "c4f6a8b02d13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("admitted_as", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "admitted_as")
