import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class PhoneNumberStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    released = "released"


class PhoneNumber(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "phone_numbers"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    retell_phone_number_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # E.164
    area_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    country: Mapped[str] = mapped_column(String(4), default="US", nullable=False)
    monthly_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[PhoneNumberStatus] = mapped_column(
        Enum(PhoneNumberStatus, name="phone_number_status"), default=PhoneNumberStatus.active, nullable=False
    )
