import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WorkspaceSearchRun(Base):
    __tablename__ = "workspace_search_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    query_text: Mapped[str] = mapped_column(Text)
    query_overrides_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    filters_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    sources_json: Mapped[list] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    stats_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    saved_search_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="SET NULL"), default=None
    )  # M39 F2


class WorkspaceSearchCursor(Base):
    __tablename__ = "workspace_search_cursors"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace_search_runs.id", ondelete="CASCADE"), primary_key=True)
    source: Mapped[str] = mapped_column(String, primary_key=True)
    cursor_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    exhausted: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class WorkspaceSearchHit(Base):
    __tablename__ = "workspace_search_hits"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspace_search_runs.id", ondelete="CASCADE"))
    external_ref_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("external_refs.id", ondelete="SET NULL"), default=None)
    source_method: Mapped[str] = mapped_column(String)
    seed_paper_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"), default=None)
    snowball_round: Mapped[int | None] = mapped_column(default=None)
    normalized_title: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    stage1_status: Mapped[str | None] = mapped_column(String, default=None)
    stage1_exclude_reason: Mapped[str | None] = mapped_column(String, default=None)
    stage1_note: Mapped[str | None] = mapped_column(Text, default=None)
    priority: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    topic_fit: Mapped[str | None] = mapped_column(String, default=None)
    acquisition_status: Mapped[str] = mapped_column(String, default="not_attempted")
    paper_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"), default=None)
    # M31: when stage1_status was last set (NULL when unset or decided before M31), for the stop hint (spec §3.2).
    stage1_decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    # M31b: the model's suggestion; never a decision (spec §4.5).
    suggestion: Mapped[str | None] = mapped_column(String, default=None)
    suggestion_reason: Mapped[str | None] = mapped_column(String, default=None)
    suggestion_note: Mapped[str | None] = mapped_column(Text, default=None)
    suggestion_model: Mapped[str | None] = mapped_column(Text, default=None)
    suggested_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
    suggestion_relevance: Mapped[int | None] = mapped_column(SmallInteger, default=None)  # M39 F3, 1-5
    suggestion_quotes: Mapped[list | None] = mapped_column(JSONB, default=None)
    suggestion_confidence: Mapped[str | None] = mapped_column(Text, default=None)
    suggestion_flags: Mapped[list | None] = mapped_column(JSONB, default=None)
    suggestion_prompt_version: Mapped[int | None] = mapped_column(SmallInteger, default=None)


class SearchRunEligibility(Base):
    __tablename__ = "search_run_eligibility"

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    search_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspace_search_runs.id", ondelete="CASCADE"), primary_key=True
    )
    stage2_status: Mapped[str | None] = mapped_column(Text, default=None)
    stage2_exclude_reason: Mapped[str | None] = mapped_column(Text, default=None)
    assessed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), default=None)
