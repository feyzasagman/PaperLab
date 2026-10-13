import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, SmallInteger, Table, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    description: Mapped[str | None] = mapped_column(Text, default=None)  # M39 F1
    # M31 (spec §3.5, §4.1, §4.2).
    screening_criteria: Mapped[str | None] = mapped_column(Text, default=None)
    screening_ranked_used: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    suggest_status: Mapped[str] = mapped_column(Text, server_default=text("'idle'"))
    suggest_done: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    suggest_total: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    suggest_error: Mapped[str | None] = mapped_column(Text, default=None)
    # M39 F3: what the model screens against, its settings, and the run the job is scoped to (null = whole pool).
    research_question: Mapped[str | None] = mapped_column(Text, default=None)
    inclusion_criteria: Mapped[str | None] = mapped_column(Text, default=None)
    exclusion_criteria: Mapped[str | None] = mapped_column(Text, default=None)
    prescreen_after_scheduled_runs: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    auto_import_min_relevance: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    suggest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("workspace_search_runs.id", ondelete="SET NULL"), default=None
    )


# A Core Table like note_anchors: a pure link, only inserted (ON CONFLICT DO NOTHING), deleted and read in bulk.
workspace_papers = Table(
    "workspace_papers",
    Base.metadata,
    Column("workspace_id", UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
    Column("paper_id", UUID(as_uuid=True), ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("added_at", DateTime(timezone=True), server_default=text("now()")),
)
