import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text, update

from app.config import settings
from app.core import discovery, paper_sources, queue, screening
from app.core.workspace_search import PER_SOURCE_PASS_LIMIT, SEARCH_RUN_JOB_TIMEOUT, get_run, search_batch
from app.db import SessionLocal
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchCursor, WorkspaceSearchRun
from app.providers import discovery_fake

logger = logging.getLogger(__name__)

# A flat pause between full-batch passes (one page from every source happens together each iteration), enough to
# stay under arXiv's ~3 req/3s and Crossref's ~1 req/s guidance (spec §6) without tuning each source separately.
BATCH_PACING_SECONDS = 2.0
# Backstop for a real run: spec §16 targets ~3,000 rows/run, and page sizes 20-100 across ~5 sources per
# iteration comfortably cover that within this many iterations. Hitting the cap before every source naturally
# exhausts is a valid terminal state (a bounded scan that found what it found), not a failure.
MAX_BATCH_ITERATIONS = 200
# ARQ's own default job_timeout is 300s. Registered as this job's actual job_timeout in workers/settings.py
# (imported from here, not redefined there, so the two can never drift apart). Bounding the loop by iteration
# count alone doesn't bound it by wall-clock time — 200 iterations x (pacing + a handful of per-source HTTP
# calls that can each take several seconds) can land in the same ballpark as this timeout on a broad query, so
# the loop also checks a wall-clock deadline below (SEARCH_RUN_SAFETY_MARGIN) to make sure it always finishes
# well inside this window, turning this into a true backstop instead of a race the loop might lose.
# ponytail: a hard kill mid-commit (e.g. a true ARQ-level cancellation past even this longer timeout) could still
# rarely leave a run stuck at "running" — full cancellation-safe cleanup (asyncio.shield around the final status
# write) is a known ceiling, not solved here, since the deadline check below makes it very unlikely in practice.
# Margin subtracted from SEARCH_RUN_JOB_TIMEOUT for the loop's own wall-clock deadline (C2 Important 1) — enough
# room for one in-flight batch's HTTP calls plus the final commit to finish after the deadline trips.
SEARCH_RUN_SAFETY_MARGIN = 60

_SECRET_PARAM = re.compile(r"(?i)((?:api_)?key=)[^&\s']+")
_ERROR_MAX_CHARS = 200

_TRY_LOCK = text("SELECT pg_try_advisory_xact_lock(:key)")


def _now() -> float:
    """A thin wrapper around time.monotonic(), so a test can monkeypatch just this module's own clock reads
    without touching the real stdlib `time` module — asyncio's own event loop calls `time.monotonic()`
    internally (e.g. for `loop.time()`), so patching it globally would also scramble asyncio's own scheduling
    during a test, not just this function's deadline math."""
    return time.monotonic()


def _lock_key(run_id: uuid.UUID) -> int:
    """Postgres advisory locks take an int8/int4 key; a UUID is 128 bits, so this folds it down to 63 bits (fits
    a signed bigint). A hash collision only ever makes an unrelated run wait its turn — it can't corrupt data —
    so this doesn't need to be collision-proof."""
    return run_id.int & 0x7FFFFFFFFFFFFFFF


async def _try_lock(session, run_id: uuid.UUID) -> bool:
    """Non-blocking and transaction-scoped: released automatically on the next commit/rollback, so it only ever
    covers one batch iteration, not the whole run. False means another worker's iteration is already in flight
    for this run right now (e.g. a stop-then-immediate-restart race, since enqueue_job has no dedup job id) — the
    caller should back off instead of racing it to the same insert (I2)."""
    return bool(await session.scalar(_TRY_LOCK, {"key": _lock_key(run_id)}))


async def _mark_exhausted_unless_stopped(
    session, run: WorkspaceSearchRun, cut_short: bool = False, expected_pass: int | None = None
) -> None:
    """The loop's shared terminal-state write for both the natural (all-cursors) exit and the bounded-backstop
    (iteration-cap or wall-clock deadline) exit. Re-checks status fresh first: a concurrent Stop landing during
    the last batch already set a status that must not be clobbered back to "exhausted" (bundled minor). Likewise a
    Continue that landed after this job's last commit started a newer pass (expected_pass is this job's), which
    this job must not end."""
    await session.refresh(run)
    if run.status == "running" and (expected_pass is None or run.stats_json.get("passes", 1) == expected_pass):
        if cut_short:
            # D266: the wall-clock or iteration backstop ended the run, not every source running dry: say so, and
            # name the sources still paging, because PRISMA "identified" is then a partial count. Status stays
            # "exhausted" for compatibility; the fields are additive.
            cursors = (await session.scalars(
                select(WorkspaceSearchCursor).where(
                    WorkspaceSearchCursor.run_id == run.id, WorkspaceSearchCursor.exhausted.is_(False)
                ).order_by(WorkspaceSearchCursor.source)
            )).all()
            # a paused source is waiting for Continue, not cut short
            unfinished = [c.source for c in cursors if not c.cursor_json.get("paused")]
            run.stats_json = {**run.stats_json, "cut_short": True, "unfinished_sources": unfinished}
        run.status = "exhausted"
        run.stopped_at = datetime.now(timezone.utc)
        await session.commit()
        run_id, workspace_id = run.id, run.workspace_id
        try:  # a pre-screening hiccup must not turn a finished run into a failed one
            if await screening.prescreen_after_run(session, run) is None:
                await queue.enqueue("suggest_screening", str(workspace_id))
        except Exception:
            logger.exception("pre-screening after run %s did not start", run_id)
            await session.rollback()
            await session.execute(  # the start was committed before the enqueue: undo it so Start still works
                update(Workspace).where(Workspace.id == workspace_id).values(
                    suggest_status="idle", suggest_run_id=None,
                    suggest_error="Couldn't start pre-screening after the scheduled search",
                )
            )
            await session.commit()


async def run_workspace_search(ctx: dict, run_id: str) -> None:
    """ARQ job: pages a search run's sources, one search_batch() call per iteration, until every source is
    exhausted, the run's status stops being "running" (checked fresh from the DB each iteration, so a
    concurrent stop-run call is noticed), MAX_BATCH_ITERATIONS is hit, or a wall-clock deadline
    (SEARCH_RUN_JOB_TIMEOUT - SEARCH_RUN_SAFETY_MARGIN) passes — both bounded backstops, not expected to matter
    in practice (see C2). ctx["transport"] is a test's MockTransport."""
    rid = uuid.UUID(run_id)
    async with SessionLocal() as session:
        try:
            run = await get_run(session, rid)
            if run.status != "running":
                return  # already stopped before this job ran — nothing to do
            my_pass = run.stats_json.get("passes", 1)

            sources = await paper_sources.get(session)
            transport = ctx.get("transport")
            if transport is None and settings.discovery_provider == "fake":
                transport = discovery_fake.transport()
            providers = discovery.build_providers(sources, transport)
            try:
                # Computed once, before the first iteration, so a slow first batch still counts against it — an
                # iteration count alone doesn't bound wall-clock time (Important 1).
                deadline = _now() + SEARCH_RUN_JOB_TIMEOUT - SEARCH_RUN_SAFETY_MARGIN
                for iteration in range(MAX_BATCH_ITERATIONS):
                    if iteration > 0:
                        await asyncio.sleep(BATCH_PACING_SECONDS)
                    await session.refresh(run)
                    if run.status != "running" or run.stats_json.get("passes", 1) != my_pass:
                        return  # stopped, or a Continue started a newer pass that a newer job owns
                    if _now() >= deadline:
                        break  # wall-clock backstop — falls through to the shared terminal-state write below
                    if not await _try_lock(session, rid):
                        logger.info("workspace search run %s already being processed elsewhere; backing off", rid)
                        return

                    result = await search_batch(session, providers, run)
                    per_source_raw = dict(run.stats_json.get("per_source_raw_count", {}))
                    for source, count in result.raw_counts.items():
                        per_source_raw[source] = per_source_raw.get(source, 0) + count
                    per_source_out = dict(run.stats_json.get("per_source_out_of_range", {}))
                    for source, count in result.out_of_range.items():
                        per_source_out[source] = per_source_out.get(source, 0) + count
                    per_source_error = dict(run.stats_json.get("per_source_error", {}))
                    for source, error in result.errors.items():
                        per_source_error[source] = _SECRET_PARAM.sub(r"\1***", error)[:_ERROR_MAX_CHARS]
                        logger.warning("workspace search run %s: %s failed: %s", rid, source, per_source_error[source])
                    capped = [s for s in result.sources_exhausted if s in result.errors]  # exhausted by the error cap
                    failed_sources = sorted({*run.stats_json.get("failed_sources", []), *capped})
                    # includes out-of-range items (unlike per_source_raw_count), so year-filtered sources still pause
                    pass_raw = dict(run.stats_json.get("pass_raw_count", {}))
                    for source, count in result.raw_counts.items():
                        pass_raw[source] = pass_raw.get(source, 0) + count + result.out_of_range.get(source, 0)
                    run.stats_json = {
                        **run.stats_json,
                        "pass_raw_count": pass_raw,
                        "per_source_error": per_source_error,
                        "failed_sources": failed_sources,
                        "last_batch_new_hits": result.new_hits,
                        "per_source_raw_count": per_source_raw,
                        "per_source_out_of_range": per_source_out,
                    }

                    cursors = (
                        (
                            await session.execute(
                                select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for c in cursors:  # the per-pass limit: pause (not exhaust) a source that has had its share
                        if not c.exhausted and pass_raw.get(c.source, 0) >= PER_SOURCE_PASS_LIMIT:
                            c.cursor_json = {**c.cursor_json, "paused": True}
                    done = bool(cursors) and all(c.exhausted or c.cursor_json.get("paused") for c in cursors)
                    if done:
                        paused = sorted(c.source for c in cursors if not c.exhausted)
                        run.stats_json = {**run.stats_json, "paused_sources": paused}
                    await session.commit()
                    if done:
                        await _mark_exhausted_unless_stopped(session, run, expected_pass=my_pass)
                        return
                # Reached by running out of iterations or hitting the wall-clock deadline without every source
                # naturally exhausting — still a valid terminal state (C2 part 3), not a failure.
                await _mark_exhausted_unless_stopped(session, run, cut_short=True, expected_pass=my_pass)
            finally:
                await providers.aclose()
        except Exception:
            logger.exception("workspace search run %s failed", rid)
            await session.rollback()
            await session.execute(
                update(WorkspaceSearchRun)
                .where(WorkspaceSearchRun.id == rid)
                .values(status="failed", stopped_at=datetime.now(timezone.utc))
            )
            await session.commit()
