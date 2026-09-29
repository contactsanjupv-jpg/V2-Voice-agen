"""
Identity & tenancy: users, organizations, membership/roles.

Every tenant-scoped table elsewhere in the app carries organization_id as a
FK. Authorization always resolves organization_id from the authenticated
session server-side (see app/auth/deps.py) — it is never taken from the
request body or URL for authorization purposes.
"""
import enum
import uuid

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class OrgRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    member = "member"


class User(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    memberships: Mapped[list["OrganizationMember"]] = relationship(back_populates="user")


class Organization(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(64), default="trial", nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    members: Mapped[list["OrganizationMember"]] = relationship(back_populates="organization")


class OrganizationMember(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_member"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[OrgRole] = mapped_column(Enum(OrgRole, name="org_role"), nullable=False, default=OrgRole.member)
    invited_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")


ROLE_RANK = {OrgRole.member: 0, OrgRole.admin: 1, OrgRole.owner: 2}


def role_at_least(role: OrgRole, minimum: OrgRole) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]
