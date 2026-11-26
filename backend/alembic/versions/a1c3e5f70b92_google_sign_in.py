"""google sign-in: oauth_identities, users.password_hash nullable

Revision ID: a1c3e5f70b92
Revises: e6b8d0f24a68
Create Date: 2026-11-15

Additive. One identity per (provider, subject) and one per (user, provider), enforced by the database. Existing users keep their password hashes. Downgrade refuses to run while any password-less (Google-only)
user exists, because restoring NOT NULL would otherwise fail or require inventing passwords.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1c3e5f70b92"
down_revision = "e6b8d0f24a68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=True)
    op.create_table(
        "oauth_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),
    )
    op.create_index("ix_oauth_identities_user_id", "oauth_identities", ["user_id"])


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from users where password_hash is null")).scalar()
    if n:
        raise RuntimeError(f"{n} user(s) have no password (Google-only); cannot restore NOT NULL. Remove or give them a password first.")
    op.drop_index("ix_oauth_identities_user_id", table_name="oauth_identities")
    op.drop_table("oauth_identities")
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=False)
