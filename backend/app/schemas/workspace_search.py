import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.source_registry import DISCOVERY_SOURCE_IDS

# Mirrors the DB CHECK constraints on workspace_search_hits (migration 0013): Literal + Field bounds reject a
# garbage value with a clean 422 in the schema layer, before it can reach the DB as an IntegrityError/500.
Stage1Status = Literal["relevant", "not_relevant", "maybe"]
ExcludeReason = Literal["wrong_topic", "wrong_study_type", "duplicate", "language", "inaccessible", "other"]
TopicFit = Literal["same_topic", "related_topic", "different_topic", "out_of_scope"]
AcquisitionStatus = Literal["not_attempted", "queued", "imported", "failed", "manual"]
# Search/discovery sources only (spec §6) — Unpaywall is DOI-only enrichment, never fanned out to by search_batch
# (app/core/paper_sources.py's own comment: "Unpaywall only adds PDF links"). Derived from the registry's own
# `is_discovery_source` flag (Phase 0), not hand-listed — a source present here but missing from
# source_registry.PAGE_FUNCS is now impossible by construction, closing the C1 bug class structurally.
SearchSource = Literal[*DISCOVERY_SOURCE_IDS]


class SearchRunCreate(BaseModel):
    query: str
    filters: dict = {}
    # An empty list has no cursors to page, so the worker would spin through MAX_BATCH_ITERATIONS doing nothing
    # before exiting — harmless but wasteful; reject it instead (bundled minor).
    sources: list[SearchSource] = Field(min_length=1)
    query_overrides: dict = {}


def _continuation(status: str, stats: dict, left: list[str]) -> dict:
    """What the UI and the tools need to offer Continue: a run that isn't running and still has sources to page."""
    return {
        "can_continue": status != "running" and bool(left), "sources_left": left,
        "paused_sources": stats.get("paused_sources", []),
    }


class SearchRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    query_text: str
    filters_json: dict
    query_overrides_json: dict
    sources_json: list[str]
    status: str
    started_at: datetime
    stopped_at: datetime | None
    stats_json: dict
    # Filled by workspace_search.run_out from the run's cursors (defaults: a bare run row still validates).
    can_continue: bool = False
    sources_left: list[str] = []
    paused_sources: list[str] = []  # sources that hit the per-pass limit in the latest pass

    def with_continuation(self, left: list[str]) -> "SearchRunOut":
        return self.model_copy(update=_continuation(self.status, self.stats_json, left))


class SearchRunSummary(BaseModel):
    """A run without its full stats_json, so a list of them stays small."""

    id: uuid.UUID
    query_text: str
    status: str
    started_at: datetime
    stopped_at: datetime | None
    sources_json: list[str]
    per_source_raw_count: dict[str, int]
    failed_sources: list[str]
    hit_count: int
    can_continue: bool = False
    sources_left: list[str] = []
    paused_sources: list[str] = []

    @classmethod
    def from_run(cls, run, hit_count: int, left: list[str] | None = None) -> "SearchRunSummary":
        stats = run.stats_json or {}
        return cls(
            **_continuation(run.status, stats, left or []),
            id=run.id, query_text=run.query_text, status=run.status, started_at=run.started_at,
            stopped_at=run.stopped_at, sources_json=run.sources_json,
            per_source_raw_count=stats.get("per_source_raw_count", {}),
            failed_sources=stats.get("failed_sources", []), hit_count=hit_count,
        )


class HitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    external_ref_id: uuid.UUID | None
    source_method: str
    normalized_title: str
    first_seen_at: datetime
    stage1_status: str | None
    stage1_exclude_reason: str | None
    stage1_note: str | None
    priority: int | None
    topic_fit: str | None
    acquisition_status: str
    paper_id: uuid.UUID | None
    # From the hit's linked ExternalRef (I7): the real title/authors/year/venue/doi are one join away, so Manual
    # acquisition and stage-1 screening don't have to show only `normalized_title`. None when external_ref_id is
    # None (defaults let a bare WorkspaceSearchHit still validate without these looked up).
    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    abstract: str | None = None
    # Every provider that has ever matched this hit's linked ExternalRef, trust-order first — [] when
    # external_ref_id is None, same as the other linked-ref fields above.
    sources: list[str] = []
    # M59 triage: what the providers said about where the paper was published; peer_review comes from source_type only.
    source_type: str | None = None
    published_doi: str | None = None
    published_venue: str | None = None
    peer_review: Literal["true", "false", "unknown"] = "unknown"
    # From the hit's linked SearchRunEligibility row (paper_id, run_id): stage-2 screening verdict, one join away
    # (Task 4). None when no verdict has been recorded yet for this hit's own run.
    stage2_status: str | None = None
    stage2_exclude_reason: str | None = None
    # M31b: the local model's suggestion, never a decision (spec §4.5).
    suggestion: str | None = None
    suggestion_reason: str | None = None
    suggestion_note: str | None = None
    suggestion_model: str | None = None
    suggested_at: datetime | None = None
    suggestion_relevance: int | None = None
    suggestion_quotes: list[str] | None = None
    suggestion_confidence: str | None = None
    suggestion_flags: list[str] | None = None
    suggestion_prompt_version: int | None = None


class HitListOut(BaseModel):
    items: list[HitOut]
    next_cursor: str | None


class RankedHitsOut(BaseModel):
    """Unscreened hits, most likely relevant first (M31a). No cursor: the order changes after every decision."""

    items: list[HitOut]
    trained: bool
    total_unscreened: int
    streak: int
    threshold: int
    show_stop_hint: bool


class ScreeningStateOut(BaseModel):
    criteria: str | None  # = inclusion_criteria, for the old UI
    research_question: str | None = None
    inclusion_criteria: str | None = None
    exclusion_criteria: str | None = None
    prescreen_after_scheduled_runs: bool = True
    auto_import_min_relevance: int | None = None
    suggest_run_id: uuid.UUID | None = None
    ranked_used: bool
    suggest_status: Literal["idle", "running", "stopping"]
    suggest_done: int
    suggest_total: int
    suggest_error: str | None
    capped: str | None = None  # D265: "200 of 8226" when the last auto pre-screen left hits for a manual Suggest
    capped_left: int | None = None
    model_label: str | None
    model_is_local: bool | None
    model_host: str | None


class ScreeningCriteriaUpdate(BaseModel):
    criteria: str | None = Field(default=None, max_length=4000)  # core.screening.CRITERIA_MAX_CHARS; = inclusion
    research_question: str | None = Field(default=None, max_length=4000)
    inclusion_criteria: str | None = Field(default=None, max_length=4000)
    exclusion_criteria: str | None = Field(default=None, max_length=4000)
    prescreen_after_scheduled_runs: bool | None = None
    auto_import_min_relevance: Literal[4, 5] | None = None  # None = off


class SuggestStart(BaseModel):
    confirm_remote: bool = False
    run_id: uuid.UUID | None = None


class AcceptSuggestions(BaseModel):
    hit_ids: list[uuid.UUID] = Field(max_length=5000)


class AcceptSuggestionsOut(BaseModel):
    accepted: int
    skipped: list[uuid.UUID]


def _reason_required_on_exclude(status: str | None, reason: str | None) -> None:
    if status == "not_relevant" and not reason:
        raise ValueError("stage1_exclude_reason is required when stage1_status is not_relevant")


class HitReviewUpdate(BaseModel):
    stage1_status: Stage1Status | None = None
    stage1_exclude_reason: ExcludeReason | None = None
    stage1_note: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    topic_fit: TopicFit | None = None

    @model_validator(mode="after")
    def exclude_needs_a_reason(self):
        _reason_required_on_exclude(self.stage1_status, self.stage1_exclude_reason)
        return self


class BulkHitReviewUpdate(BaseModel):
    hit_ids: list[uuid.UUID]
    stage1_status: Stage1Status
    stage1_exclude_reason: ExcludeReason | None = None
    priority: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def exclude_needs_a_reason(self):
        _reason_required_on_exclude(self.stage1_status, self.stage1_exclude_reason)
        return self


class BulkUpdateOut(BaseModel):
    updated: int


class ImportHitsRequest(BaseModel):
    hit_ids: list[uuid.UUID] | None = None


class ImportHitsOut(BaseModel):
    imported: int
    failed: int


class ClearHitsOut(BaseModel):
    deleted: int


class SnowballRequest(BaseModel):
    # An empty seed_paper_ids AND empty seed_hit_ids has no seed to hop from, so it would produce a run with zero
    # results and nothing recorded — reject it instead (same "reject rather than silently do nothing" convention
    # as SearchRunCreate.sources). Neither list alone is required (min_length=1 on just one would make the other
    # mandatory) — the model_validator below checks the combination.
    seed_paper_ids: list[uuid.UUID] = Field(default_factory=list)
    seed_hit_ids: list[uuid.UUID] = Field(default_factory=list)
    backward: bool = True
    forward: bool = True

    @model_validator(mode="after")
    def at_least_one_seed(self):
        if not self.seed_paper_ids and not self.seed_hit_ids:
            raise ValueError("at least one seed_paper_id or seed_hit_id is required")
        return self


class SnowballOut(BaseModel):
    new_hits: int
    skipped_seeds: list[uuid.UUID]
    errors: dict[str, str]


class EligibilityUpdate(BaseModel):
    status: Literal["include", "exclude"]
    # Free-text, unlike stage1's CHECK'd ExcludeReason enum — SearchRunEligibility.stage2_exclude_reason is a
    # plain TEXT column with no DB-level CHECK constraint (Task 1).
    exclude_reason: str | None = None


class EligibilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    paper_id: uuid.UUID
    search_run_id: uuid.UUID
    stage2_status: str | None
    stage2_exclude_reason: str | None
    assessed_at: datetime | None


class PrismaExportOut(BaseModel):
    identified: int
    duplicates_removed: int
    stage1_screened: int
    stage1_excluded: int
    stage1_excluded_by_reason: dict[str, int]
    sought: int
    not_retrieved: int
    stage2_assessed: int
    stage2_excluded: int
    stage2_excluded_by_reason: dict[str, int]
    included: int
    runs: list[dict]
    automation: list[str] = []
    notes: list[str] = []
    screening_criteria: str | None = None
