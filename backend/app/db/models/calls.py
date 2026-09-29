import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class CallDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"
    test = "test"


class Call(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "calls"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    retell_call_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    direction: Mapped[CallDirection] = mapped_column(Enum(CallDirection, name="call_direction"), nullable=False)
    caller_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    disconnect_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # NEVER a public URL — a storage key we resolve to a short-lived signed URL at read time.
    recording_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CallTranscript(Base, UUIDPKMixin, TimestampMixin):
    """
    Kept separate from `calls` so transcript retention/pruning can run on a
    different policy than call metadata (spec: "don't unnecessarily log full
    transcript data").
    """
    __tablename__ = "call_transcripts"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True, unique=True
    )
    transcript: Mapped[dict] = mapped_column(JSONB, nullable=False)
