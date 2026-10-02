"""agent: retell_llm_id + synced_version

Revision ID: c3e5a7b91d24
Revises: b8d2f4a61c30
"""
import sqlalchemy as sa
from alembic import op

revision = "c3e5a7b91d24"
down_revision = "b8d2f4a61c30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("retell_llm_id", sa.String(length=128), nullable=True))
    op.create_unique_constraint("uq_agents_retell_llm_id", "agents", ["retell_llm_id"])
    op.add_column("agents", sa.Column("synced_version", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("agents", "synced_version")
    op.drop_constraint("uq_agents_retell_llm_id", "agents", type_="unique")
    op.drop_column("agents", "retell_llm_id")