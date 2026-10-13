import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EvidenceCard(Base):
    """One paper's evidence card (M39 F5): status generating | ready | failed, `fields` in the spec §2 shape,
    provenance llm | llm_edited, and the llm_outputs row it came from."""

    __tablename__ = "evidence_cards"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    fields: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    provenance: Mapped[str] = mapped_column(Text, server_default=text("'llm'"))
    model: Mapped[str | None] = mapped_column(Text, default=None)
    prompt_version: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    output_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_outputs.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
