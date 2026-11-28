"""data retention audit trail

Revision ID: d4f6b8c03e25
Revises: c3e5a7b92d14
Create Date: 2026-11-27

Adds `data_retention_runs` (counts only). Nothing existing is altered, so rollback is a plain drop.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d4f6b8c03e25"
down_revision = "c3e5a7b92d14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_retention_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows_affected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
    )
    op.create_index("ix_data_retention_runs_org_created", "data_retention_runs", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_data_retention_runs_org_created", table_name="data_retention_runs")
    op.drop_table("data_retention_runs")
