import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, SmallInteger, Text, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, aggregate_order_by
from sqlalchemy.orm import Mapped, column_property, mapped_column

from app.models.base import Base
from app.models.workspace import workspace_papers

# Schema lives in alembic/versions; these mappings cover the columns the app reads/writes.
# chunks.tsv is a generated column and deliberately unmapped.


class PaperStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    ENRICHING = "enriching"
    READY = "ready"
    FAILED = "failed"


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    doi: Mapped[str | None] = mapped_column(Text, unique=True)
    openalex_id: Mapped[str | None] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    # The byline: display names in order, never an identity. Identities live in authors/paper_authors (Q3).
    authors: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'"))
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    # M39 F6: see app.core.source_types. evidence_access is derived (the property below), never stored.
    source_type: Mapped[str | None] = mapped_column(Text)
    peer_reviewed: Mapped[str] = mapped_column(Text, server_default=text("'unknown'"))
    published_doi: Mapped[str | None] = mapped_column(Text)
    is_retracted: Mapped[bool] = mapped_column(server_default=text("false"))
    oa_status: Mapped[str | None] = mapped_column(Text)
    oa_url: Mapped[str | None] = mapped_column(Text)
    cited_by_count: Mapped[int | None]
    referenced_works_count: Mapped[int | None]
    issn: Mapped[str | None] = mapped_column(Text)
    # Columns the user corrected by hand. Extraction and enrichment never overwrite them.
    manual_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    file_path: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int | None]
    status: Mapped[str] = mapped_column(Text, server_default=PaperStatus.UPLOADED)
    status_error: Mapped[str | None] = mapped_column(Text)
    # M7.5: the reference fetch's own state, separate from ingest. requested_at lets a stuck "fetching" be retried.
    references_state: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    references_error: Mapped[str | None] = mapped_column(Text)
    references_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # M21 (D119): the reader's own record, written only by papers.set_reading. Passes finished, 0–3, and keep / later /
    # drop or None. Never in manual_fields, never a workspace, never written by ingest, enrichment or MCP.
    reading_pass: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    triage: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    # Loaded with every paper, so paper cards can tick their workspaces. Oldest membership first.
    workspace_ids: Mapped[list[uuid.UUID]] = column_property(
        select(
            func.coalesce(
                func.array_agg(aggregate_order_by(workspace_papers.c.workspace_id, workspace_papers.c.added_at)),
                text("'{}'::uuid[]"),
            )
        )
        .where(workspace_papers.c.paper_id == id)
        .correlate_except(workspace_papers)
        .scalar_subquery()
    )

    @property
    def evidence_access(self) -> str:
        from app.core.source_types import evidence_access

        return evidence_access(self.abstract, self.status, self.has_chunks)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    ordinal: Mapped[int]
    page: Mapped[int]
    bbox: Mapped[list[list[float]]] = mapped_column(JSONB)
    section_title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any | None] = mapped_column(Vector(768))
    embed_model: Mapped[str] = mapped_column(Text)
    strategy_ver: Mapped[int]


# True when the paper has chunks; loaded with every paper so evidence_access needs no query.
Paper.has_chunks = column_property(
    select(func.count(Chunk.id) > 0).where(Chunk.paper_id == Paper.id).correlate_except(Chunk).scalar_subquery()
)
