import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class DataRetentionRun(Base, UUIDPKMixin, TimestampMixin):
    """
    Audit trail of automatic pruning (services/retention.py): one row per organization per pass that changed anything
    or failed. It holds COUNTS and the cutoff only, never call content, numbers or names. Deleted with the organization.
    """

    __tablename__ = "data_retention_runs"
    __table_args__ = (Index("ix_data_retention_runs_org_created", "organization_id", "created_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # "transcripts" | "caller_data"
    cutoff: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    rows_affected: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # "ok" | "failed"
