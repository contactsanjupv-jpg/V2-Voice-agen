"""after-hours routing: structured schedule, closed twin ids, lead flag

Revision ID: e6b8d0f24a68
Revises: d5a7b9c13e24
Create Date: 2026-11-10

All columns are nullable with no default, so existing rows are untouched and nothing changes until an owner opts in.
Rollback (downgrade) drops them; the closed twin Retell resources, if any exist, must be deleted separately.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e6b8d0f24a68"
down_revision = "d5a7b9c13e24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("hours_schedule", postgresql.JSONB(), nullable=True))
    op.add_column("agents", sa.Column("closed_retell_agent_id", sa.String(128), nullable=True))
    op.add_column("agents", sa.Column("closed_retell_llm_id", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_agents_closed_retell_agent_id", "agents", ["closed_retell_agent_id"])
    op.create_unique_constraint("uq_agents_closed_retell_llm_id", "agents", ["closed_retell_llm_id"])
    op.add_column("leads", sa.Column("received_after_hours", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "received_after_hours")
    op.drop_constraint("uq_agents_closed_retell_llm_id", "agents", type_="unique")
    op.drop_constraint("uq_agents_closed_retell_agent_id", "agents", type_="unique")
    op.drop_column("agents", "closed_retell_llm_id")
    op.drop_column("agents", "closed_retell_agent_id")
    op.drop_column("businesses", "hours_schedule")
