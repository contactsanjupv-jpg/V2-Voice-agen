import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class BusinessStatus(str, enum.Enum):
    draft = "draft"
    reviewed = "reviewed"
    active = "active"


class Business(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "businesses"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[BusinessStatus] = mapped_column(
        Enum(BusinessStatus, name="business_status"), default=BusinessStatus.draft, nullable=False
    )
    # Exactly what the scraper found, before any human edit — kept for audit /
    # so we can always show "here's what changed from what we imported."
    raw_import_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    approved_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    knowledge_items: Mapped[list["KnowledgeItem"]] = relationship(back_populates="business")


class KnowledgeItemType(str, enum.Enum):
    service = "service"
    faq = "faq"
    policy = "policy"
    custom = "custom"


class KnowledgeItemSource(str, enum.Enum):
    import_ = "import"
    manual = "manual"


class KnowledgeItem(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "knowledge_items"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[KnowledgeItemType] = mapped_column(Enum(KnowledgeItemType, name="knowledge_item_type"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[KnowledgeItemSource] = mapped_column(Enum(KnowledgeItemSource, name="knowledge_item_source"), nullable=False)

    business: Mapped["Business"] = relationship(back_populates="knowledge_items")
