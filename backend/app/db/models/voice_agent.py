import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class Voice(Base, UUIDPKMixin, TimestampMixin):
    """
    Shared catalog, NOT tenant-scoped — mirrors Retell's list-voices response.
    Refreshed periodically by a background worker; never written to directly
    by a tenant request.
    """
    __tablename__ = "voices"

    retell_voice_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # elevenlabs, openai, playht, cartesia, deepgram
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    accent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    age_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    voice_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AgentStatus(str, enum.Enum):
    draft = "draft"
    testing = "testing"
    active = "active"
    paused = "paused"


class Agent(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "agents"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    voice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("voices.id"), nullable=True)

    # Null until the first successful create-agent call against Retell.
    retell_agent_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True, index=True)
    # The Retell LLM (response engine) that holds the prompt + tools. An agent
    # without one has no instructions and no business knowledge.
    retell_llm_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    greeting: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    personality: Mapped[str] = mapped_column(String(64), default="professional", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en-US", nullable=False)

    # {"answer_questions": true, "capture_leads": true, "book_appointments": false,
    #  "transfer_calls": false, "take_messages": true}
    tasks: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    transfer_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    business_hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_hours_behavior: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status"), default=AgentStatus.draft, nullable=False
    )
    # `version` bumps on every saved change; `synced_version` is the version
    # Retell has actually received. synced_version < version means our saved
    # config is NOT what callers currently hear.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    synced_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    voice: Mapped["Voice"] = relationship()
