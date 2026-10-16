import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Integer, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExternalRef(Base):
    """A paper some library paper cites or is cited by. Not a paper: no chunks, no status (addendum §3b).

    `external_ids` (Phase 0b) is the canonical per-source identifier map going forward — keyed by
    app.core.source_registry's own source ids (`"openalex"`, `"semantic_scholar"`, `"arxiv"`, `"core"`), not the
    legacy column names. `s2_id`/`openalex_id`/`arxiv_id`/`core_id` below are kept mapped (read by nothing new,
    written by nothing new) so fixtures built before this migration — and unrelated features' own fixtures that
    never matched by identifier in the first place — keep working unchanged; they are dropped in a later migration.
    """

    __tablename__ = "external_refs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    s2_id: Mapped[str | None] = mapped_column(Text, unique=True)
    openalex_id: Mapped[str | None] = mapped_column(Text, unique=True)
    doi: Mapped[str | None] = mapped_column(Text)
    arxiv_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(Text)
    cited_by_count: Mapped[int | None]
    abstract: Mapped[str | None] = mapped_column(Text, default=None)
    pdf_urls: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    title_embedding: Mapped[Any | None] = mapped_column(Vector(768))
    title_embed_model: Mapped[str | None] = mapped_column(Text)
    # Set once on import: every paper citing this reference then shows it in the library.
    imported_as: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"))
    # M21 (D120, D164): when the reader marked it To read. A fold keeps the earliest; an import clears it.
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    core_id: Mapped[str | None] = mapped_column(Text, default=None)
    # Every provider that has ever matched this paper, trust-order first (Candidate.sources' own convention) —
    # union'd across every merge that touches this row, never just the most recent one.
    sources: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    external_ids: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=text("'{}'"))
    # M59 hit triage: filled at search time from what the providers return; null when no source said.
    source_type: Mapped[str | None] = mapped_column(Text)
    published_doi: Mapped[str | None] = mapped_column(Text)
    published_venue: Mapped[str | None] = mapped_column(Text)
    # When enrich_hits last asked Semantic Scholar about this ref, whatever it answered: asked once only.
    triage_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


paper_references = Table(
    "paper_references",
    Base.metadata,
    Column("paper_id", UUID(as_uuid=True), ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("ref_id", UUID(as_uuid=True), ForeignKey("external_refs.id", ondelete="CASCADE"), primary_key=True),
    Column("direction", Text, primary_key=True),
    Column("position", Integer, nullable=False),
)


class NoteEmbedding(Base):
    """A note's vector for ranking references by closeness to what the reader writes about (D81)."""

    __tablename__ = "note_embeddings"

    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    embedding: Mapped[Any] = mapped_column(Vector(768))
    embed_model: Mapped[str] = mapped_column(Text)
    noted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
