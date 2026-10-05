"""durable phone provisioning (one number per org)

Revision ID: f6b8d0e42a57
Revises: e5a7c9d31f46
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6b8d0e42a57"
down_revision = "e5a7c9d31f46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phone_provisionings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("nickname", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("country", sa.String(4), nullable=False),
        sa.Column("area_code", sa.String(8), nullable=True),
        sa.Column("phone_number_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("phone_provisionings")
