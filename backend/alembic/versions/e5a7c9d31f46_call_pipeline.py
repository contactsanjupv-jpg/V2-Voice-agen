"""call pipeline: webhook retry state, usage ledger, one lead per call

Revision ID: e5a7c9d31f46
Revises: d4f6b8c20e35
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5a7c9d31f46"
down_revision = "d4f6b8c20e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("webhook_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("webhook_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_leads_call", "leads", ["call_id"], unique=True, postgresql_where=sa.text("call_id IS NOT NULL")
    )
    op.drop_table("usage_records")
    op.create_table(
        "usage_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("retell_call_id", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("billable_seconds", sa.Integer(), nullable=False),
        sa.Column("provider_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("provider_cost_raw", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_ledger_organization_id", "usage_ledger", ["organization_id"])
    op.create_index("ix_usage_ledger_occurred_at", "usage_ledger", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("usage_ledger")
    op.create_table(
        "usage_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minutes_used", sa.Integer(), nullable=False),
        sa.Column("calls_count", sa.Integer(), nullable=False),
        sa.Column("retell_cost_cents_estimate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_index("uq_leads_call", table_name="leads")
    op.drop_column("webhook_events", "next_attempt_at")
    op.drop_column("webhook_events", "last_error")
    op.drop_column("webhook_events", "attempts")
