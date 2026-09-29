import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class IntegrationProvider(str, enum.Enum):
    google_calendar = "google_calendar"
    calendly = "calendly"
    cal_com = "cal_com"


class IntegrationStatus(str, enum.Enum):
    connected = "connected"
    disconnected = "disconnected"
    error = "error"


class Integration(Base, UUIDPKMixin, TimestampMixin):
    """
    OAuth tokens are encrypted at rest (see app/core/crypto.py) and are only
    ever decrypted inside the relevant BookingProvider adapter — never
    returned to the frontend, never logged.
    """
    __tablename__ = "integrations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[IntegrationProvider] = mapped_column(Enum(IntegrationProvider, name="integration_provider"), nullable=False)
    status: Mapped[IntegrationStatus] = mapped_column(
        Enum(IntegrationStatus, name="integration_status"), default=IntegrationStatus.disconnected, nullable=False
    )
    access_token_encrypted: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    connected_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
