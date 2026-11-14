"""business timezone (IANA name; NULL until the owner sets it)

Revision ID: c4f6a8b02d13
Revises: b2e4f6a80c91
"""
import sqlalchemy as sa
from alembic import op

revision = "c4f6a8b02d13"
down_revision = "b2e4f6a80c91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("timezone", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("businesses", "timezone")
