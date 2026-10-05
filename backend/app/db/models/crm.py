import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class LeadStatus(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    booked = "booked"
    closed = "closed"


class Lead(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "leads"
    __table_args__ = (Index("uq_leads_call", "call_id", unique=True, postgresql_where="call_id IS NOT NULL"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus, name="lead_status"), default=LeadStatus.new, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="call", nullable=False)


class AppointmentStatus(str, enum.Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    rescheduled = "rescheduled"


class Appointment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "appointments"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True
    )
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    starts_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"), default=AppointmentStatus.confirmed, nullable=False
    )
