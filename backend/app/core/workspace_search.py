"""Fetch one page per source, merge and dedup against the library-outside pool, store new hits (M30a spec P2).

Reuses candidates.py's merge/dedup (`merge`, `_same_paper`, `normal_title`) exactly as the Find Papers flow
(`core/discovery.py`) does — a workspace search hit is a candidate that survived the same merge logic, stored
instead of returned. `search_batch()` takes a `WorkspaceSearchRun` row (not raw params) so a future cron-driven
caller (M7.6) can build one the same way this plan's worker does.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import delete, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import ai_sort, discovery, year_range
from app.core.candidates import Candidate, from_s2, merge, normal_title
from app.core.discovery import Providers, _add_unpaywall_links, download_pdf
from app.core.references import _same_reference
from app.core.source_registry import MAPPERS as _MAPPERS
from app.core.source_registry import PAGE_FUNCS as _PAGE_FUNCS
from app.core.source_registry import PAGE_SIZE_BY_SOURCE
from app.core.source_registry import STARTING_CURSOR_VALUE as _STARTING_CURSOR_VALUE
from app.models.references import ExternalRef, paper_references
from app.models.workspace_search import (
    SearchRunEligibility,
    WorkspaceSearchCursor,
    WorkspaceSearchHit,
    WorkspaceSearchRun,
)
from app.providers import semantic_scholar

if TYPE_CHECKING:
    from app.schemas.workspace_search import HitReviewUpdate

# A source stuck on httpx errors (spec §16: unauthenticated S2 search 429s by default) retries this many times
# before its cursor is marked exhausted instead of spinning forever (C2 part 1) — small enough that a real outage
# still lets the run finish on its other sources well inside a job's lifetime.
#
# Retries are spaced out (below), not consecutive: without spacing, all SOURCE_ERROR_CAP attempts land within a
# few seconds of each other (one worker-loop pacing interval apart, BATCH_PACING_SECONDS in
# workers/workspace_search.py), so a source that's just having a brief, transient blip — arXiv's own API answers
# a plain 406 every so often for no discernible reason, confirmed by re-running an identical, otherwise-successful
# request seconds later — gets abandoned for the rest of the run before the blip could ever clear. Growing
# cooldowns give a real chance for that, while a source that's persistently broken (an unauthenticated S2/CORE
# 429, expected by default per spec §16) still gives up for good once every backoff step is spent.
SOURCE_ERROR_CAP = 6
# The search job's timeout (see workers/workspace_search.py); here so core (saved_searches) needn't import workers.
SEARCH_RUN_JOB_TIMEOUT = 1800
_ERROR_BACKOFF_SECONDS = (10, 30, 90, 180, 300)


@dataclass(frozen=True)
class BatchResult:
    new_hits: int
    sources_exhausted: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # Per-source raw item counts for this one batch, pre-merge/pre-dedup — spec §5/§13 wants these for M30b's
    # PRISMA "identified" number, which can't be reconstructed later once a run is exhausted (I1). The worker
    # accumulates these into run.stats_json across iterations; search_batch itself stays stateless.
    raw_counts: dict[str, int] = field(default_factory=dict)
    out_of_range: dict[str, int] = field(default_factory=dict)  # M39 F2: dropped by the run's year range (D221)


async def _find_or_create_external_ref(session: AsyncSession, candidate: Candidate) -> ExternalRef:
    """The OR-match below can hit more than one row (e.g. one row shares the candidate's DOI, a different row
    shares its semantic_scholar id) — `.first()` on an unordered result would pick one arbitrarily, and a wrong
    pick doesn't just risk a bad backfill (handled below), it attaches this hit to the wrong paper outright
    (Important 2 from the fix-round review: a candidate importable later would then download/attach the WRONG
    row's PDF). DOI is the strongest identifier, so when the candidate has one, a DOI-matching row is ordered
    first — ties among several DOI matches (shouldn't happen; doi has no DB-level uniqueness) or candidates with
    no DOI fall back to whatever order the DB happens to return, same as before.

    Backfilling an identifier onto whichever row is picked is only safe when no OTHER row already owns the same
    value: otherwise it either violates the partial unique indexes on external_ids->>'openalex'/'semantic_scholar'
    (Phase 0b), or — for doi/arxiv/core, which have no DB uniqueness — silently mis-attributes an identifier to
    the wrong paper (spec review I3; this exact mechanism corrupted a real row once, see the ledger's "Closed
    Access Fixture" incident). `pdf_urls` gets its own, stricter check: it only crosses over when the matched
    row's own DOI doesn't contradict the candidate's, so a match found only via a weaker/shared identifier never
    hands one paper's PDF link to a different paper."""
    conditions = [
        ExternalRef.external_ids[source].astext == value for source, value in candidate.external_ids.items()
    ]
    if candidate.doi:
        conditions.append(ExternalRef.doi == candidate.doi)
    existing = None
    if conditions:
        query = select(ExternalRef).where(or_(*conditions))
        if candidate.doi:
            # `.is_(True)` turns a NULL-doi row's comparison (SQL NULL, not FALSE) into a definite boolean first —
            # otherwise Postgres' NULLS-FIRST-on-DESC default would rank a no-DOI row above an actual DOI match.
            query = query.order_by((ExternalRef.doi == candidate.doi).is_(True).desc())
        existing = (await session.execute(query)).scalars().first()
    if existing is not None:
        external_ids = dict(existing.external_ids)
        for source, value in candidate.external_ids.items():
            if source in external_ids:
                continue  # `existing` already has its own value for this source
            owned_elsewhere = await session.scalar(
                select(ExternalRef.id).where(
                    ExternalRef.external_ids[source].astext == value, ExternalRef.id != existing.id
                )
            )
            if owned_elsewhere is None:
                external_ids[source] = value
        if external_ids != existing.external_ids:
            existing.external_ids = external_ids
        if candidate.doi and not existing.doi:
            owned_elsewhere = await session.scalar(
                select(ExternalRef.id).where(ExternalRef.doi == candidate.doi, ExternalRef.id != existing.id)
            )
            if owned_elsewhere is None:
                existing.doi = candidate.doi
        if candidate.pdf_urls and not existing.pdf_urls:
            doi_conflict = existing.doi and candidate.doi and existing.doi != candidate.doi
            if not doi_conflict:
                existing.pdf_urls = candidate.pdf_urls
        if candidate.cited_by_count and not existing.cited_by_count:
            existing.cited_by_count = candidate.cited_by_count
        if candidate.abstract and not existing.abstract:
            existing.abstract = candidate.abstract
        # Union, not backfill-if-empty like the fields above: a later search or snowball round can rediscover an
        # already-known paper through a source that hadn't found it before, and every one of those is worth
        # keeping, not just whichever happened to create the row.
        if candidate.sources:
            existing.sources = list(dict.fromkeys([*existing.sources, *candidate.sources]))
        return existing
    ref = ExternalRef(
        title=candidate.title,
        doi=candidate.doi,
        external_ids=dict(candidate.external_ids),
        authors=candidate.authors,
        year=candidate.year,
        venue=candidate.venue,
        cited_by_count=candidate.cited_by_count,
        abstract=candidate.abstract,
        pdf_urls=candidate.pdf_urls,
        sources=list(candidate.sources),
    )
    session.add(ref)
    await session.flush()
    return ref


def _now() -> datetime:
    """A thin wrapper around datetime.now(timezone.utc), so a test can monkeypatch just this module's own clock
    reads for the error-backoff check below without needing a real sleep — same reasoning as
    workers/workspace_search.py's own _now() wrapper (that one for a wall-clock deadline, this one for a
    per-source retry cooldown)."""
    return datetime.now(timezone.utc)


async def search_batch(session: AsyncSession, providers: Providers, run: WorkspaceSearchRun) -> BatchResult:
    """One page per source in `run.sources_json`, merged and deduped, new hits stored. A source that fails (a
    provider's search_page raising httpx.HTTPError, same as discovery.py's existing search()) is recorded in
    `errors` and its cursor gets `last_error`; the run and its other sources are unaffected (spec P7)."""
    cursors = {
        c.source: c
        for c in (
            await session.execute(select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id))
        ).scalars()
    }

    found: dict[str, list[Candidate]] = {}
    errors: dict[str, str] = {}
    exhausted: list[str] = []
    raw_counts: dict[str, int] = {}
    years = year_range.parse(run.filters_json)
    out_of_range: dict[str, int] = {}

    now = _now()
    for source in run.sources_json:
        cursor = cursors.get(source)
        if cursor is None or cursor.exhausted:
            continue
        retry_after = cursor.cursor_json.get("retry_after")
        if retry_after and now < datetime.fromisoformat(retry_after):
            continue  # cooling down from a recent error — not a failure, not exhausted, just not due yet
        client = providers.client(source)
        if client is None:
            # Disabled/unconfigured (e.g. Unpaywall with no contact_email — the fresh-database default): nothing
            # to fetch here, not an error. Mark it exhausted now so the worker's all(c.exhausted ...) check can
            # still become true once every other source has genuinely exhausted (M30a Task 17 E2E bug).
            cursor.last_error = None
            cursor.exhausted = True
            exhausted.append(source)
            continue
        # P11: a per-source override (query_overrides_json) takes over that source's query; otherwise the run's
        # own query_text, same as before. Recorded on the run so a later PRISMA export reflects what was actually
        # searched (I6), not just what the caller intended.
        query_for_source = run.query_overrides_json.get(source) or run.query_text
        try:
            raw_items, next_cursor = await _PAGE_FUNCS[source](
                client, query_for_source, PAGE_SIZE_BY_SOURCE[source], cursor.cursor_json.get("value", 0)
            )
        except httpx.HTTPError as exc:
            cursor.last_error = str(exc)
            errors[source] = str(exc)
            # Bounded retry (C2 part 1): a source stuck on httpx errors (e.g. an unauthenticated S2 429 — the
            # default case per spec §16's spike, not an edge case) must eventually stop retrying instead of
            # spinning until the worker's job_timeout kills it mid-commit. Spaced out (see SOURCE_ERROR_CAP's
            # comment), not consecutive, so a brief/transient error gets a real chance to clear before the source
            # is abandoned for the rest of the run.
            error_count = cursor.cursor_json.get("errors", 0) + 1
            if error_count >= SOURCE_ERROR_CAP:
                cursor.exhausted = True
                exhausted.append(source)
                cursor.cursor_json = {**cursor.cursor_json, "errors": error_count}
            else:
                backoff = _ERROR_BACKOFF_SECONDS[min(error_count - 1, len(_ERROR_BACKOFF_SECONDS) - 1)]
                cursor.cursor_json = {
                    **cursor.cursor_json,
                    "errors": error_count,
                    "retry_after": (now + timedelta(seconds=backoff)).isoformat(),
                }
            continue
        # `from_crossref` returns `None` for a non-paper record (D73's Crossref filter); the other four mappers
        # always return a Candidate. `is not None` below is a no-op for those four and the real filter for crossref.
        mapped = [c for raw in raw_items if (c := _MAPPERS[source](raw)) is not None]
        # ponytail: filtered here, not by each source's own year parameter, so out-of-range pages still count toward
        # the run's iteration cap; pass native year params per source if broad runs hit the cap (D221).
        found[source] = [c for c in mapped if years is None or years.keeps(c.year)]
        out_of_range[source] = len(mapped) - len(found[source])
        raw_counts[source] = len(raw_items) - out_of_range[source]
        cursor.last_error = None
        if next_cursor is None:
            cursor.exhausted = True
            exhausted.append(source)
        else:
            # A fresh dict with no "errors" key resets the count to 0 (`.get("errors", 0)` above) — a page that
            # succeeds clears whatever error streak came before it.
            cursor.cursor_json = {"value": next_cursor}

    new_hits = await _store_candidates_as_hits(session, run, "database_search", found)

    return BatchResult(new_hits=new_hits, sources_exhausted=exhausted, errors=errors, raw_counts=raw_counts,
                       out_of_range=out_of_range)


async def _store_candidates_as_hits(
    session: AsyncSession, run: WorkspaceSearchRun, source_method: str, found: dict[str, list[Candidate]],
    seed_paper_id: uuid.UUID | None = None, snowball_round: int | None = None,
) -> int:
    """Merges candidates from every source, dedups against the existing pool by (workspace_id, external_ref_id),
    and stores new ones as hits. Shared by search_batch (source_method="database_search") and snowball
    (source_method="snowball_backward"/"snowball_forward") so both features use exactly one hit-creation/dedup
    path — a paper already in the pool (this run, an earlier one, or a prior snowball hop) keeps its existing
    screening state (spec P8, §9)."""
    all_candidates = [c for group in found.values() for c in group]
    merged = merge(found, len(all_candidates)) if all_candidates else []

    new_hits = 0
    for candidate in merged:
        ref = await _find_or_create_external_ref(session, candidate)
        existing_hit = (
            (
                await session.execute(
                    select(WorkspaceSearchHit).where(
                        WorkspaceSearchHit.workspace_id == run.workspace_id,
                        WorkspaceSearchHit.external_ref_id == ref.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_hit is not None:
            continue  # already in the pool (this run or an earlier one) — keeps its existing screening decision
        if await _insert_hit(
            session, run, ref, candidate, source_method=source_method,
            seed_paper_id=seed_paper_id, snowball_round=snowball_round,
        ):
            new_hits += 1
    return new_hits


async def _insert_hit(
    session: AsyncSession, run: WorkspaceSearchRun, ref: ExternalRef, candidate: Candidate,
    source_method: str = "database_search", seed_paper_id: uuid.UUID | None = None,
    snowball_round: int | None = None,
) -> bool:
    """Inserts a new pool hit for `ref`, or no-ops if the DB's own `(workspace_id, external_ref_id)` unique
    constraint already has a row for it — not just this function's own pre-check SELECT above, which two workers
    racing the same run (e.g. a stop-then-immediate-restart, since enqueue_job has no dedup job id) can both pass
    before either commits. Returns whether a row was actually inserted (I2)."""
    inserted_id = await session.scalar(
        pg_insert(WorkspaceSearchHit)
        .values(
            workspace_id=run.workspace_id,
            run_id=run.id,
            external_ref_id=ref.id,
            source_method=source_method,
            seed_paper_id=seed_paper_id,
            snowball_round=snowball_round,
            normalized_title=normal_title(candidate.title),
            first_seen_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "external_ref_id"])
        .returning(WorkspaceSearchHit.id)
    )
    return inserted_id is not None


from app.core import paper_sources, papers, workspaces
from app.core.errors import Conflict, InvalidInput, NotFound
from app.models import Paper, workspace_papers

# OpenAlex's `page` cursor is 1-based; every other source's is a 0-based offset (Task 2's convention — the
# engine tests' own START_CURSOR already knows this). Seeding OpenAlex at 0 asked its fake for a negative offset
# and got nothing back "by coincidence," hiding the bug (I4).

async def start_run(
    session: AsyncSession, workspace_id, query_text: str, filters: dict, sources: list[str],
    query_overrides: dict | None = None, run_id=None,
) -> WorkspaceSearchRun:
    await workspaces.get(session, workspace_id)  # raises NotFound if missing
    if run_id is not None:
        run = await get_run(session, run_id)
        if run.workspace_id != workspace_id:
            raise NotFound(f"search run {run_id} not found")
        if run.status == "running":
            raise Conflict("search_run_already_running")
        run.status = "running"
        run.stopped_at = None
        await session.commit()
        return run

    years = year_range.parse(filters)  # raises InvalidInput("invalid_years")
    filters = years.as_filters() if years else {}

    # D73's merge trust order (app/core/paper_sources.SOURCES) is what search_batch's `found` dict — insertion
    # ordered, fed straight into merge()'s ranking — relies on; storing sources in whatever order the caller sent
    # them silently let the caller's field order become the trust order instead.
    ordered_sources = [source for source in paper_sources.SOURCES if source in sources]

    run = WorkspaceSearchRun(
        workspace_id=workspace_id, query_text=query_text, filters_json=filters,
        query_overrides_json=query_overrides or {}, sources_json=ordered_sources, status="running",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    for source in ordered_sources:
        seed = {"value": _STARTING_CURSOR_VALUE.get(source, 0)}
        session.add(WorkspaceSearchCursor(run_id=run.id, source=source, cursor_json=seed))
    await session.commit()
    return run


async def stop_run(session: AsyncSession, run_id, workspace_id=None) -> WorkspaceSearchRun:
    run = await get_run(session, run_id, workspace_id)
    if run.status != "running":
        # Stopping only applies to a run that's actually in flight — same "doesn't apply to the current state"
        # convention as start_run's own Conflict above, not a silent no-op that could paper over a stale double
        # click.
        raise Conflict("search_run_not_running")
    run.status = "stopped"
    run.stopped_at = datetime.now(timezone.utc)
    await session.commit()
    return run


async def get_run(session: AsyncSession, run_id, workspace_id=None) -> WorkspaceSearchRun:
    run = await session.get(WorkspaceSearchRun, run_id)
    if run is None or (workspace_id is not None and run.workspace_id != workspace_id):
        raise NotFound(f"search run {run_id} not found")
    return run


# Own constants, deliberately not references.py's REFS_CAP/CITING_CAP — the two features stay decoupled even
# though the values happen to match today.
SNOWBALL_REFS_CAP = 500
SNOWBALL_CITING_CAP = 200


@dataclass(frozen=True)
class SnowballResult:
    new_hits: int
    skipped_seeds: list[uuid.UUID] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


async def _snowball_from_s2(
    providers: Providers, doi: str | None, title: str, backward: bool, forward: bool
) -> tuple[dict[str, list[Candidate]], dict[str, str]] | None:
    """None when Semantic Scholar doesn't know the doi/title (a 404 on any direction asked for — same signal
    references.py's _from_semantic_scholar uses). Mirrors that function, scoped to just the requested
    direction(s). Takes doi/title directly rather than a Paper, so this serves either an imported library paper or
    a not-yet-imported hit's own ExternalRef (snowball's point, below).

    Otherwise returns whatever direction(s) succeeded, plus a per-direction message for whichever raised
    httpx.HTTPError (e.g. an unauthenticated S2 429 — spec §16's default, not an edge case). Each direction is
    tried and caught independently: one direction erroring must never drop the other direction's already-fetched
    hits (fix-round finding — /citations 429ing used to wipe out a successful /references call for the same
    seed, because the exception used to propagate out of this whole function to snowball()'s per-seed
    try/except)."""
    key = await discovery.s2_key(providers.client("semantic_scholar"), doi, title)
    if key is None:
        return None
    out: dict[str, list[Candidate]] = {}
    errors: dict[str, str] = {}
    if backward:
        try:
            cites = await semantic_scholar.references(providers.client("semantic_scholar"), key, SNOWBALL_REFS_CAP)
        except httpx.HTTPError as exc:
            errors["semantic_scholar_backward"] = str(exc)
        else:
            if cites is None:
                return None
            out["backward"] = [from_s2(p) for p in cites]
    if forward:
        try:
            cited_by = await semantic_scholar.citations(providers.client("semantic_scholar"), key, SNOWBALL_CITING_CAP)
        except httpx.HTTPError as exc:
            errors["semantic_scholar_forward"] = str(exc)
        else:
            if cited_by is None:
                return None
            out["forward"] = [from_s2(p) for p in cited_by]
    return out, errors


async def snowball(
    session: AsyncSession, providers: Providers, workspace_id: uuid.UUID, seed_paper_ids: list[uuid.UUID],
    seed_hit_ids: list[uuid.UUID], backward: bool, forward: bool,
) -> SnowballResult:
    """One hop, from each seed, via Semantic Scholar. A seed can be an already-imported library paper
    (seed_paper_ids) or a not-yet-imported workspace hit (seed_hit_ids) — citation-graph discovery only ever
    needs a doi/title (discovery.s2_key), which a hit's own ExternalRef already has, so snowballing from it
    never requires a free PDF to have been found first (that's import_hits' separate concern: acquiring a copy
    of the paper, not discovering what it cites/is cited by). New hits land in the same pool as database-search
    hits (spec P8, §9), tagged snowball_backward/snowball_forward, deduped by (workspace_id, external_ref_id)
    through the same `_store_candidates_as_hits` path search_batch uses — a seed's reference already in the pool
    keeps its existing screening state.

    WorkspaceSearchHit.run_id is NOT NULL, and a snowball hop isn't a polling "run" — a lightweight run row is
    created once per call (status="exhausted" immediately: there's nothing to page) and used as every resulting
    hit's run_id, which also gives PRISMA's later per-run reporting a real row to point at.

    Semantic Scholar entirely off (providers.client("semantic_scholar") is None) raises Conflict once, up
    front, mirroring references.fetch()'s "nothing enabled" check — with no source configured at all there's no
    useful per-seed distinction to make (every seed would just land in skipped_seeds for the same reason)."""
    if providers.client("semantic_scholar") is None:
        raise Conflict("Semantic Scholar is off. Turn it on in Settings → Paper sources to snowball.")

    run = WorkspaceSearchRun(
        workspace_id=workspace_id,
        query_text=f"snowball ({len(seed_paper_ids) + len(seed_hit_ids)} seed paper(s))",
        filters_json={}, query_overrides_json={}, sources_json=["semantic_scholar"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()

    skipped: list[uuid.UUID] = []
    errors: dict[str, str] = {}
    new_hits = 0
    for paper_id in seed_paper_ids:
        member = (
            await session.execute(
                select(workspace_papers.c.paper_id).where(
                    workspace_papers.c.workspace_id == workspace_id, workspace_papers.c.paper_id == paper_id
                )
            )
        ).first()
        if member is None:
            raise NotFound(f"paper {paper_id} not in workspace {workspace_id}")
        paper = await session.get(Paper, paper_id)
        try:
            result = await _snowball_from_s2(providers, paper.doi, paper.title, backward, forward)
        except httpx.HTTPError as exc:
            # A failure before either direction's own try/except could run (e.g. the s2_key title-match lookup
            # itself 429ing) — not a per-direction error, so no direction-specific key to give it.
            errors["semantic_scholar"] = str(exc)
            continue
        if result is None:
            skipped.append(paper_id)
            continue
        found, direction_errors = result
        errors.update(direction_errors)
        for direction, candidates in found.items():
            source_method = "snowball_backward" if direction == "backward" else "snowball_forward"
            new_hits += await _store_candidates_as_hits(
                session, run, source_method, {"semantic_scholar": candidates},
                seed_paper_id=paper_id, snowball_round=1,
            )

    for hit_id in seed_hit_ids:
        hit = await session.get(WorkspaceSearchHit, hit_id)
        if hit is None or hit.workspace_id != workspace_id:
            raise NotFound(f"hit {hit_id} not found in workspace {workspace_id}")
        ref = await session.get(ExternalRef, hit.external_ref_id) if hit.external_ref_id else None
        if ref is None:
            skipped.append(hit_id)
            continue
        try:
            result = await _snowball_from_s2(providers, ref.doi, ref.title, backward, forward)
        except httpx.HTTPError as exc:
            errors["semantic_scholar"] = str(exc)
            continue
        if result is None:
            skipped.append(hit_id)
            continue
        found, direction_errors = result
        errors.update(direction_errors)
        for direction, candidates in found.items():
            source_method = "snowball_backward" if direction == "backward" else "snowball_forward"
            # ponytail: no seed_hit_id column on WorkspaceSearchHit — seed_paper_id stays unset for a hit-seeded
            # hop (nothing to attribute to a Paper row that doesn't exist yet). Add one if that provenance is
            # ever needed; grep confirms nothing reads seed_paper_id downstream today.
            new_hits += await _store_candidates_as_hits(
                session, run, source_method, {"semantic_scholar": candidates}, snowball_round=1,
            )

    await session.commit()
    return SnowballResult(new_hits=new_hits, skipped_seeds=skipped, errors=errors)


async def set_eligibility(
    session: AsyncSession, workspace_id: uuid.UUID, paper_id: uuid.UUID, run_id: uuid.UUID,
    status: str, exclude_reason: str | None,
) -> SearchRunEligibility:
    """Upserts one (paper_id, run_id) stage-2 verdict in a single INSERT ... ON CONFLICT DO UPDATE, insert on the
    first PATCH, update in place on a later one for the same pair. A `session.get` then conditional `session.add`
    (the previous approach) has a race window between the SELECT and the write where two concurrent PATCHes for
    the same pair can both see no row and both try to insert — one loses to a duplicate-PK IntegrityError. A
    single upsert statement closes that window, same discipline as `_insert_hit`'s upsert above, on this table's
    own (paper_id, search_run_id) composite PK. Same ownership discipline as review_hit/get_run — confirms both
    the run (get_run, which already raises NotFound on a workspace mismatch or missing run) and the paper
    (workspace_papers membership, same check snowball() above uses) belong to workspace_id before writing."""
    await get_run(session, run_id, workspace_id)
    member = (await session.execute(
        select(workspace_papers.c.paper_id).where(
            workspace_papers.c.workspace_id == workspace_id, workspace_papers.c.paper_id == paper_id
        )
    )).first()
    if member is None:
        raise NotFound(f"paper {paper_id} not in workspace {workspace_id}")

    assessed_at = datetime.now(timezone.utc)
    stmt = (
        pg_insert(SearchRunEligibility)
        .values(
            paper_id=paper_id, search_run_id=run_id, stage2_status=status,
            stage2_exclude_reason=exclude_reason, assessed_at=assessed_at,
        )
        .on_conflict_do_update(
            index_elements=["paper_id", "search_run_id"],
            set_={"stage2_status": status, "stage2_exclude_reason": exclude_reason, "assessed_at": assessed_at},
        )
        .returning(SearchRunEligibility)
        # populate_existing: an UPDATE branch's RETURNING row must overwrite an already-identity-mapped instance
        # (e.g. this same (paper_id, run_id) loaded earlier in this session) with the fresh DB values — without
        # it, the ORM leaves an already-loaded object's attributes as they were and the caller sees stale data.
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return row


def _encode_cursor(first_seen_at: datetime, hit_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([first_seen_at.isoformat(), str(hit_id)]).encode()).decode()


_HIT_COLUMNS = [column.name for column in WorkspaceSearchHit.__table__.columns]


def _hit_out_dict(
    hit: WorkspaceSearchHit, ref: ExternalRef | None,
    stage2_status: str | None = None, stage2_exclude_reason: str | None = None,
) -> dict:
    """A hit's own columns plus its linked ExternalRef's title/authors/year/venue/doi/abstract/sources, merged into
    one dict for HitOut (I7). This codebase's models never use relationship() (a manual join/lookup is the
    convention), so the caller passes in whichever `ref` it already has — a join row here, an explicit
    session.get elsewhere — and this just does the merge, once, the same way for all three HitOut-producing call
    sites below.

    stage2_status/stage2_exclude_reason come from the hit's SearchRunEligibility row (paper_id, run_id), when one
    exists — only list_hits looks it up (the Screening tab drives off the list); review_hit/upload_hit_pdf are
    stage-1/acquisition mutations, not stage-2 actions, so they leave these at their None default rather than
    adding a fetch nothing reads."""
    data = {name: getattr(hit, name) for name in _HIT_COLUMNS}
    data.update(
        title=ref.title if ref else None,
        authors=ref.authors if ref else None,
        year=ref.year if ref else None,
        venue=ref.venue if ref else None,
        doi=ref.doi if ref else None,
        abstract=ref.abstract if ref else None,
        sources=ref.sources if ref else [],
        stage2_status=stage2_status,
        stage2_exclude_reason=stage2_exclude_reason,
    )
    return data


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Raises InvalidInput for any malformed cursor — bad base64/UTF-8, bad JSON, wrong shape, or a
    seen_at/hit_id that don't parse as a datetime/UUID — so a tampered `after` value is a clean 422."""
    try:
        seen_at, hit_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(seen_at), uuid.UUID(hit_id)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise InvalidInput("invalid pagination cursor") from exc


async def list_hits(
    session: AsyncSession, workspace_id, limit: int = 50, after: str | None = None,
    stage1_status: str | None = None, acquisition_status: str | None = None, run_id: uuid.UUID | None = None,
    sort: str | None = None, min_relevance: int | None = None, suggested_only: bool = False,
) -> tuple[list[dict], str | None]:
    """Keyset-paginated hit listing, ordered by (first_seen_at, id) so the cursor is stable even when several
    hits share a first_seen_at timestamp. `tuple_()` on both sides makes SQLAlchemy emit a real SQL row-value
    comparison (`WHERE (first_seen_at, id) > (:seen_at, :id)`) instead of a no-op Python tuple comparison.

    Outer-joins ExternalRef (I7) since external_ref_id is nullable — a hit with no linked ref still comes back,
    with title/authors/year/venue/doi all None via `_hit_out_dict`. Also outer-joins SearchRunEligibility on
    (paper_id, run_id) so the Screening tab (a later task) can drive its list off this same endpoint, filtered to
    acquisition_status IN ('imported', 'manual') — a hit with no stage-2 verdict yet still comes back, with
    stage2_status/stage2_exclude_reason None."""
    query = (
        select(WorkspaceSearchHit, ExternalRef, SearchRunEligibility)
        .join(ExternalRef, WorkspaceSearchHit.external_ref_id == ExternalRef.id, isouter=True)
        .join(
            SearchRunEligibility,
            (SearchRunEligibility.paper_id == WorkspaceSearchHit.paper_id)
            & (SearchRunEligibility.search_run_id == WorkspaceSearchHit.run_id),
            isouter=True,
        )
        .where(WorkspaceSearchHit.workspace_id == workspace_id)
    )
    if stage1_status == "undecided":
        query = query.where(WorkspaceSearchHit.stage1_status.is_(None))
    elif stage1_status:
        query = query.where(WorkspaceSearchHit.stage1_status == stage1_status)
    if suggested_only:
        query = query.where(WorkspaceSearchHit.suggestion.is_not(None))
    if run_id is not None:
        query = query.where(WorkspaceSearchHit.run_id == run_id)
    if acquisition_status:
        query = query.where(WorkspaceSearchHit.acquisition_status == acquisition_status)
    if min_relevance is not None:
        if not 1 <= min_relevance <= 5:
            raise InvalidInput("min_relevance must be 1 to 5")
        query = query.where(WorkspaceSearchHit.suggestion_relevance >= min_relevance)
    if sort not in (None, "ai"):
        raise InvalidInput("sort must be 'ai' or empty")
    if sort == "ai":  # offset cursor, see ai_sort
        offset = ai_sort.decode_offset(after)
        query = query.order_by(*ai_sort.AI_ORDER).offset(offset).limit(limit)
    else:
        if after:
            seen_at, hit_id = _decode_cursor(after)
            query = query.where(
                tuple_(WorkspaceSearchHit.first_seen_at, WorkspaceSearchHit.id) > tuple_(seen_at, hit_id)
            )
        query = query.order_by(WorkspaceSearchHit.first_seen_at, WorkspaceSearchHit.id).limit(limit)
    rows = (await session.execute(query)).all()
    items = [
        _hit_out_dict(
            hit, ref,
            elig.stage2_status if elig else None, elig.stage2_exclude_reason if elig else None,
        )
        for hit, ref, elig in rows
    ]
    if len(rows) < limit:
        return items, None
    if sort == "ai":
        return items, ai_sort.encode_offset(offset + limit)
    return items, _encode_cursor(rows[-1][0].first_seen_at, rows[-1][0].id)


async def review_hit(session: AsyncSession, hit_id, workspace_id, update: "HitReviewUpdate") -> dict:
    """Stage-1 screening update for one hit. Scoped to workspace_id (same ownership rule as get_run/stop_run) so a
    hit_id guessed or leaked from another workspace can't be patched through this route."""
    hit = await session.get(WorkspaceSearchHit, hit_id)
    if hit is None or hit.workspace_id != workspace_id:
        raise NotFound(f"hit {hit_id} not found")

    # The schema's model_validator only sees this request's own fields, not the hit's persisted state, so a
    # partial update that omits stage1_status (e.g. one that only nulls stage1_exclude_reason) can't be caught
    # there. Check the *effective* post-patch combination here instead.
    fields = update.model_fields_set
    new_status = update.stage1_status if "stage1_status" in fields else hit.stage1_status
    new_reason = update.stage1_exclude_reason if "stage1_exclude_reason" in fields else hit.stage1_exclude_reason
    if new_status == "not_relevant" and not new_reason:
        raise InvalidInput("stage1_exclude_reason is required when stage1_status is not_relevant")

    for field_name, value in update.model_dump(exclude_unset=True).items():
        setattr(hit, field_name, value)
    if "stage1_status" in fields:
        # The stop hint's streak reads decisions by this time (M31 spec §3.2); an undone decision has none.
        hit.stage1_decided_at = _now() if hit.stage1_status is not None else None
    await session.commit()
    ref = await session.get(ExternalRef, hit.external_ref_id) if hit.external_ref_id else None
    return _hit_out_dict(hit, ref)


async def bulk_review_hits(
    session: AsyncSession, workspace_id, hit_ids: list, stage1_status: str,
    stage1_exclude_reason: str | None, priority: int | None,
) -> int:
    """Applies the same stage1_status (and optional reason/priority) to every hit in hit_ids that belongs to
    workspace_id. Hit ids from another workspace are silently excluded from the count, not raised as an error —
    a bulk action naming one bad id shouldn't fail the whole batch."""
    result = await session.execute(
        select(WorkspaceSearchHit).where(
            WorkspaceSearchHit.id.in_(hit_ids), WorkspaceSearchHit.workspace_id == workspace_id
        )
    )
    hits = result.scalars().all()
    decided_at = _now()  # one time for the whole batch
    for hit in hits:
        hit.stage1_status = stage1_status
        hit.stage1_decided_at = decided_at
        if stage1_exclude_reason:
            hit.stage1_exclude_reason = stage1_exclude_reason
        if priority is not None:
            hit.priority = priority
    await session.commit()
    return len(hits)


async def owned_hit_ids(session: AsyncSession, workspace_id, hit_ids: list) -> set:
    """The ids among hit_ids that belong to workspace_id."""
    rows = await session.scalars(
        select(WorkspaceSearchHit.id).where(
            WorkspaceSearchHit.id.in_(hit_ids), WorkspaceSearchHit.workspace_id == workspace_id
        )
    )
    return set(rows)


async def clear_hits(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Deletes every hit in the pool that hasn't been imported or manually acquired -- a clean-slate reset for a
    workspace whose accumulated search history no longer matters. Papers already pulled into the corpus are
    untouched (their hits are excluded from the delete, same as `import_hits`' own acquisition_status check).
    Run/cursor history is left alone too — this clears the pool a run FED, not the record that the run happened."""
    await workspaces.get(session, workspace_id)  # raises NotFound if missing
    result = await session.execute(
        delete(WorkspaceSearchHit).where(
            WorkspaceSearchHit.workspace_id == workspace_id,
            WorkspaceSearchHit.acquisition_status.notin_(["imported", "manual"]),
        )
    )
    await session.commit()
    return result.rowcount


@dataclass(frozen=True)
class ImportResult:
    imported: int
    failed: int
    paper_ids: list[uuid.UUID] = field(default_factory=list)


async def _record_snowball_citation(session: AsyncSession, hit: WorkspaceSearchHit, ref_id: uuid.UUID) -> None:
    """Owner's choice (2026-10-04): once a snowball-discovered hit is acquired, record the hop as a citation edge
    so the existing Graph (paper_references + external_refs, graph.py) shows it — no separate "snowball graph"
    view needed. Only a paper-seeded hop (seed_paper_id set) has a library paper to link from; a hit-seeded hop's
    seed_paper_id stays unset (snowball()'s own ponytail note) so there's nothing to record yet.

    ponytail: this row can be overwritten by a later real references.fetch() for the seed paper (it deletes and
    replaces every row for that paper_id+direction) — acceptable, since that fetch's own Semantic Scholar/OpenAlex
    data would include the same edge anyway if it's still a real citation."""
    if hit.source_method not in ("snowball_backward", "snowball_forward") or hit.seed_paper_id is None:
        return
    direction = "cites" if hit.source_method == "snowball_backward" else "cited_by"
    await session.execute(
        pg_insert(paper_references)
        .values(paper_id=hit.seed_paper_id, ref_id=ref_id, direction=direction, position=0)
        .on_conflict_do_nothing()
    )


async def import_hits(
    session: AsyncSession, providers: Providers, workspace_id: uuid.UUID, hit_ids: list[uuid.UUID] | None
) -> ImportResult:
    """Best-effort bulk import (spec Task 9), mirroring discovery.add's one-candidate download-then-create flow:
    every targeted hit (hit_ids, or every not-yet-imported `relevant` hit) gets its own outcome, so one hit with no
    free PDF never stops the rest of the batch. A hit left `failed` is Manual acquisition's job (Task 11).
    `paper_ids` are the newly created papers, for the route to enqueue ingest on — a core function has no
    Request to enqueue with itself. A hit whose ExternalRef.imported_as is already set (import_reference's own
    bookkeeping, same as ours below) is attached to that existing paper without downloading again, and its
    paper_id is left out of paper_ids so ingest isn't re-enqueued for a paper that already went through it."""
    query = select(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == workspace_id)
    query = (
        query.where(WorkspaceSearchHit.id.in_(hit_ids))
        if hit_ids
        else query.where(WorkspaceSearchHit.stage1_status == "relevant")
    )
    hits = (await session.execute(query)).scalars().all()

    imported, failed, paper_ids = 0, 0, []
    for hit in hits:
        if hit.acquisition_status in ("imported", "manual"):
            # "manual" is its own provenance (spec §4.4/M30b's PRISMA export need it distinguished from an
            # automatic import) — re-running Import all must never quietly relabel it "imported" (I8).
            continue
        ref = await session.get(ExternalRef, hit.external_ref_id) if hit.external_ref_id else None
        if ref is not None and ref.imported_as is not None:
            # Already in the library — an earlier hit in this batch, an earlier run, or the References panel.
            # Attach the existing paper instead of downloading/creating a duplicate, and don't re-enqueue ingest.
            await workspaces.add_paper(session, workspace_id, ref.imported_as)
            hit.acquisition_status = "imported"
            hit.paper_id = ref.imported_as
            await _record_snowball_citation(session, hit, ref.id)
            imported += 1
            continue
        if ref is not None and not ref.pdf_urls and ref.doi:
            # Unpaywall's real role (spec §6): DOI-only PDF enrichment for a candidate missing one, applied
            # post-merge — never a discovery source. import_hits only ever looked at pdf_urls as already stored;
            # try this once before giving up (I5). providers.client("unpaywall") may be None (source off) — the
            # helper already treats that as a no-op, same as discovery.add()'s own callers do.
            arxiv_only = {"arxiv": ref.external_ids["arxiv"]} if "arxiv" in ref.external_ids else {}
            [enriched] = await _add_unpaywall_links(
                providers.client("unpaywall"), [Candidate(title=ref.title, doi=ref.doi, external_ids=arxiv_only)]
            )
            if enriched.pdf_urls:
                ref.pdf_urls = enriched.pdf_urls
        data = await download_pdf(providers.pdf, ref.pdf_urls) if ref and ref.pdf_urls else None
        if data is None:
            hit.acquisition_status = "failed"
            failed += 1
            continue
        paper = await papers.create_paper(
            session, f"{hit.normalized_title}.pdf", data, settings.pdf_dir, prefill={"title": ref.title}
        )
        await workspaces.add_paper(session, workspace_id, paper.id)
        same_paper = [ExternalRef.id == ref.id, *_same_reference(ref.external_ids, ref.doi)]
        await session.execute(update(ExternalRef).where(or_(*same_paper)).values(imported_as=paper.id))
        hit.acquisition_status = "imported"
        hit.paper_id = paper.id
        await _record_snowball_citation(session, hit, ref.id)
        imported += 1
        paper_ids.append(paper.id)

    await session.commit()
    return ImportResult(imported, failed, paper_ids)


async def upload_hit_pdf(
    session: AsyncSession, workspace_id: uuid.UUID, hit_id: uuid.UUID, filename: str | None, content: bytes
) -> tuple[dict, bool]:
    """Manual acquisition (spec Task 10) for a hit with no free PDF: the user supplies the file directly instead of
    `import_hits` finding one. Same ownership scoping and already-imported handling as `import_hits` above — a
    hit whose ExternalRef.imported_as is already set gets the existing paper attached, no new paper, no re-enqueue.
    Returns (hit, created) so the route only enqueues ingest when this call actually created a new paper.

    The %PDF-body check is the trust boundary for this user-uploaded file (spec's Global Constraint): reject
    before touching the already-imported branch too, using the same magic bytes `papers.create_paper` checks."""
    if not content.startswith(papers.PDF_MAGIC):
        raise InvalidInput("uploaded file is not a PDF")

    hit = await session.get(WorkspaceSearchHit, hit_id)
    if hit is None or hit.workspace_id != workspace_id:
        raise NotFound(f"hit {hit_id} not found")

    ref = await session.get(ExternalRef, hit.external_ref_id) if hit.external_ref_id else None
    if ref is not None and ref.imported_as is not None:
        await workspaces.add_paper(session, workspace_id, ref.imported_as)
        hit.acquisition_status = "manual"
        hit.paper_id = ref.imported_as
        await _record_snowball_citation(session, hit, ref.id)
        await session.commit()
        return _hit_out_dict(hit, ref), False

    paper = await papers.create_paper(
        session, filename or f"{hit.normalized_title}.pdf", content, settings.pdf_dir,
        prefill={"title": ref.title} if ref else None,
    )
    await workspaces.add_paper(session, workspace_id, paper.id)
    if ref is not None:
        same_paper = [ExternalRef.id == ref.id, *_same_reference(ref.external_ids, ref.doi)]
        await session.execute(update(ExternalRef).where(or_(*same_paper)).values(imported_as=paper.id))
    hit.acquisition_status = "manual"
    hit.paper_id = paper.id
    if ref is not None:
        await _record_snowball_citation(session, hit, ref.id)
    await session.commit()
    return _hit_out_dict(hit, ref), True
