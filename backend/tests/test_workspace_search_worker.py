"""ARQ job that pages a workspace search run to exhaustion (M30a Task 5)."""

import itertools
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import delete, select, update

from app.core.workspace_search import PAGE_SIZE_BY_SOURCE, SOURCE_ERROR_CAP
from app.models import PaperSources
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchCursor, WorkspaceSearchHit, WorkspaceSearchRun
from app.providers import discovery_fake
from app.workers import workspace_search as worker_module

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


@pytest.fixture
async def worker(session, monkeypatch):
    """Same pattern as test_references_api.py's `worker` fixture: point the job's own SessionLocal at the test's
    rolled-back session, and clear any leftover paper_sources row from the shared dev DB (D15) so the defaults
    apply (arxiv on, OpenAlex off) instead of whatever a developer last configured. Also skips the real
    BATCH_PACING_SECONDS sleep between iterations (C2 part 2) so multi-iteration tests stay fast."""
    await session.execute(delete(PaperSources))

    @asynccontextmanager
    async def shared_session():
        yield session

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker_module, "SessionLocal", shared_session)
    monkeypatch.setattr(worker_module.asyncio, "sleep", no_sleep)
    return {"transport": discovery_fake.transport()}


async def _new_run(session, status: str, sources: list[str] | None = None) -> WorkspaceSearchRun:
    workspace = Workspace(name=f"Worker test {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id,
        query_text="bert",
        filters_json={},
        query_overrides_json={},
        sources_json=sources or ["arxiv"],
        status=status,
        started_at=datetime.now(timezone.utc),
        stats_json={},
    )
    session.add(run)
    await session.flush()
    return run


async def test_worker_pages_until_exhausted(session, worker):
    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    reloaded = await session.get(WorkspaceSearchRun, run.id)
    assert reloaded.status == "exhausted"
    assert reloaded.stopped_at is not None
    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(hits) >= 1  # search_batch's own merge/dedup counting is test_workspace_search_engine.py's job


async def test_worker_pages_multiple_batches_before_exhausting(session, worker, monkeypatch):
    """PAGE_SIZE_BY_SOURCE["arxiv"] (20) exceeds discovery_fake's 3-item PAGE_PAPERS list, so a real run exhausts
    after exactly one page — test_worker_pages_until_exhausted never proves the `while True` loop calls
    search_batch a second time. Shrink the page size below the fixture's item count so arXiv needs two pages,
    and count calls to prove the loop actually iterates."""
    monkeypatch.setitem(PAGE_SIZE_BY_SOURCE, "arxiv", 2)
    real_search_batch = worker_module.search_batch
    calls = 0

    async def counting_search_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_search_batch(*args, **kwargs)

    monkeypatch.setattr(worker_module, "search_batch", counting_search_batch)

    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    assert calls == 2  # proves the loop looped: one page (2 items) then a second (1 item) before exhausting
    reloaded = await session.get(WorkspaceSearchRun, run.id)
    assert reloaded.status == "exhausted"


async def test_worker_stops_mid_loop_on_a_concurrent_status_change(session, worker, monkeypatch):
    """Simulates the (future, Task 6) stop-run API endpoint flipping `run.status` to "stopped" via a raw UPDATE
    while a batch is in flight — the loop's session.refresh(run) + status check at the top of the next pass must
    notice and exit before the source finishes paging."""
    monkeypatch.setitem(PAGE_SIZE_BY_SOURCE, "arxiv", 2)  # needs 2 pages to exhaust — see test above
    real_search_batch = worker_module.search_batch
    calls = 0

    async def stop_after_first_batch(session_arg, providers, run_arg):
        nonlocal calls
        calls += 1
        result = await real_search_batch(session_arg, providers, run_arg)
        if calls == 1:
            await session_arg.execute(
                update(WorkspaceSearchRun).where(WorkspaceSearchRun.id == run_arg.id).values(status="stopped")
            )
        return result

    monkeypatch.setattr(worker_module, "search_batch", stop_after_first_batch)

    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    assert calls == 1  # the loop never reached a second batch — it noticed the concurrent stop first
    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "stopped"  # not "exhausted" — set by the "concurrent" update, not the worker
    cursor = (
        await session.execute(select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id))
    ).scalar_one()
    assert cursor.exhausted is False  # exited early — the source never actually finished paging


async def test_worker_exhausts_with_one_disabled_source(session, worker, monkeypatch):
    """arxiv pages normally against the fake and exhausts after one page (fixture's 3 items, page size 20).
    unpaywall has no contact_email configured (the `worker` fixture clears paper_sources so SourceSettings()'s
    fresh-database default applies), so its Providers.client() is None. Before the fix, an unconfigured source's
    cursor never flips to exhausted, so `all(c.exhausted for c in cursors)` never becomes true and the loop spins
    forever. A SAFETY_CAP force-stops the run so a regression fails this test instead of hanging it."""
    SAFETY_CAP = 5
    real_search_batch = worker_module.search_batch
    calls = 0

    async def capped_search_batch(session_arg, providers, run_arg):
        nonlocal calls
        calls += 1
        result = await real_search_batch(session_arg, providers, run_arg)
        if calls >= SAFETY_CAP:
            await session_arg.execute(
                update(WorkspaceSearchRun).where(WorkspaceSearchRun.id == run_arg.id).values(status="stopped")
            )
        return result

    monkeypatch.setattr(worker_module, "search_batch", capped_search_batch)

    run = await _new_run(session, "running", sources=["arxiv", "unpaywall"])
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    session.add(WorkspaceSearchCursor(run_id=run.id, source="unpaywall", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    assert calls < SAFETY_CAP  # exhausted itself well before the safety cap had to step in
    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "exhausted"  # not "stopped" — the safety cap never fired


async def test_worker_does_nothing_for_a_run_that_is_not_running(session, worker):
    run = await _new_run(session, "stopped")
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.run_id == run.id)))
        .scalars()
        .all()
    )
    assert hits == []  # never even called search_batch, since it was already stopped


async def test_worker_marks_the_run_failed_on_an_unexpected_error(session, worker, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(worker_module, "search_batch", broken)
    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()
    run_id = run.id  # captured before the call: the worker's rollback expires `run` (test_references_api.py's pattern)

    await worker_module.run_workspace_search(worker, str(run_id))

    reloaded = await session.get(WorkspaceSearchRun, run_id, populate_existing=True)
    assert reloaded.status == "failed"
    assert reloaded.stopped_at is not None  # same bookkeeping as stop_run's own status change (bundled minor)


async def test_worker_bounds_retries_on_a_source_that_always_errors(session, worker, monkeypatch):
    """A source whose provider always raises httpx.HTTPError must not retry forever: search_batch's own error
    count on the cursor (C2 part 1) caps it at SOURCE_ERROR_CAP attempts, so the run reaches a terminal state
    instead of spinning until ARQ's job_timeout kills it mid-commit. Retries are spaced out by a growing cooldown
    (real wall-clock minutes, not iterations) so a brief/transient error gets a real chance to clear — the fake
    clock here jumps far enough each call that every cooldown is already over by the next attempt, so this test
    still runs in milliseconds despite that real-world spacing."""
    from app.core import workspace_search as core_module

    fake_clock = itertools.count(0, step=10_000)
    monkeypatch.setattr(core_module, "_now", lambda: datetime.fromtimestamp(next(fake_clock), tz=timezone.utc))

    always_429 = {"transport": httpx.MockTransport(lambda request: httpx.Response(429))}
    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(always_429, str(run.id))

    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "exhausted"  # the error cap tripped it, not the MAX_BATCH_ITERATIONS backstop
    cursor = (
        await session.execute(select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id))
    ).scalar_one()
    assert cursor.exhausted is True
    assert cursor.cursor_json.get("errors") == SOURCE_ERROR_CAP


async def test_worker_marks_exhausted_when_the_iteration_cap_is_hit(session, worker, monkeypatch):
    """A source that always finds a fresh page (never a short page, never an error) would page forever under the
    old `while True` loop. MAX_BATCH_ITERATIONS (C2 part 3) bounds it, and hitting the cap is a valid terminal
    state — exhausted, not failed — since a bounded scan that found what it found isn't an error."""
    monkeypatch.setattr(worker_module, "MAX_BATCH_ITERATIONS", 3)

    from app.core import workspace_search as core_module

    async def never_ending_page(client, query, page_size, cursor):
        return [], cursor + page_size  # a "full" page every time — next_cursor is never None, never exhausts

    monkeypatch.setitem(core_module._PAGE_FUNCS, "arxiv", never_ending_page)
    real_search_batch = worker_module.search_batch
    calls = 0

    async def counting_search_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_search_batch(*args, **kwargs)

    monkeypatch.setattr(worker_module, "search_batch", counting_search_batch)

    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    assert calls == 3  # ran exactly MAX_BATCH_ITERATIONS times, not forever
    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "exhausted"


async def test_worker_stops_via_wall_clock_deadline_before_the_iteration_cap(session, worker, monkeypatch):
    """MAX_BATCH_ITERATIONS bounds the loop by count, but nothing tied that to wall-clock time before this fix —
    200 iterations of pacing plus per-source HTTP calls can land in the same ballpark as SEARCH_RUN_JOB_TIMEOUT
    on a broad query, so the iteration cap alone might not fire before ARQ's hard kill does (Important 1). Fakes
    the clock jumping far past the deadline between the pre-loop calculation and the first iteration's check, and
    confirms the run reaches exhausted without running any batches at all."""
    # First call computes the deadline (time 0 -> deadline = 0 + 1800 - 60 = 1740); every call after that is far
    # past it, so the very first iteration's check trips. Patches the worker's own _now() wrapper, not the real
    # time.monotonic — asyncio's event loop calls that internally, so patching it globally would also scramble
    # the test's own scheduling instead of just this function's deadline math.
    fake_clock = itertools.chain([0.0], itertools.repeat(10_000.0))
    monkeypatch.setattr(worker_module, "_now", lambda: next(fake_clock))

    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "exhausted"  # the wall-clock backstop tripped it, not the iteration cap
    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.run_id == run.id)))
        .scalars()
        .all()
    )
    assert hits == []  # zero batches ran — the deadline was already past on the first iteration


async def test_mark_exhausted_unless_stopped_does_not_clobber_a_concurrent_stop(session):
    """Both of the loop's "mark exhausted" exit points share this helper specifically so a Stop that lands in the
    narrow window between the last batch's commit and this final write isn't clobbered back to "exhausted"
    (bundled minor)."""
    run = await _new_run(session, "running")
    await session.commit()
    await session.execute(update(WorkspaceSearchRun).where(WorkspaceSearchRun.id == run.id).values(status="stopped"))
    await session.commit()

    await worker_module._mark_exhausted_unless_stopped(session, run)

    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "stopped"  # not overwritten


async def test_worker_records_stats_json_after_a_batch(session, worker):
    """I1: the UI reads stats_json.last_batch_new_hits for live progress, and M30b's PRISMA export needs a
    per-source raw (pre-dedup) count that can't be reconstructed once a run is exhausted — neither was ever
    written before this fix."""
    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.stats_json["last_batch_new_hits"] == len(discovery_fake.PAGE_PAPERS)
    assert reloaded.stats_json["per_source_raw_count"] == {"arxiv": len(discovery_fake.PAGE_PAPERS)}


async def test_worker_backs_off_when_another_worker_holds_the_lock(session, worker, monkeypatch):
    """I2: a stop-then-immediate-restart race (enqueue_job has no dedup job id) can start two workers on the same
    run_id. When this worker's own advisory-lock attempt reports the run already in flight elsewhere, it must
    back off cleanly — no search_batch call, no status change, no exception."""

    async def lock_held_elsewhere(*_args, **_kwargs):
        return False

    monkeypatch.setattr(worker_module, "_try_lock", lock_held_elsewhere)
    real_search_batch = worker_module.search_batch
    calls = 0

    async def counting_search_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_search_batch(*args, **kwargs)

    monkeypatch.setattr(worker_module, "search_batch", counting_search_batch)

    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    assert calls == 0
    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "running"  # untouched — this worker never got past the lock check


# --- pre-screening after a scheduled run (M39 F3) ---------------------------------------------------------


@pytest.fixture
def enqueued(monkeypatch):
    jobs = []

    async def fake_enqueue(job, *args):
        jobs.append((job, *args))

    monkeypatch.setattr(worker_module.queue, "enqueue", fake_enqueue)
    return jobs


async def _finish_run(session, worker, *, scheduled=True, criteria="RCTs", setting=True, suggest_status="idle"):
    from app.models.saved_search import SavedSearch  # noqa: PLC0415

    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    workspace = await session.get(Workspace, run.workspace_id)
    workspace.inclusion_criteria = criteria
    workspace.prescreen_after_scheduled_runs = setting
    workspace.suggest_status = suggest_status
    if scheduled:
        saved = SavedSearch(workspace_id=workspace.id, query="bert", schedule="0 6 * * *", timezone="UTC")
        session.add(saved)
        await session.flush()
        run.saved_search_id = saved.id
    await session.commit()
    await worker_module.run_workspace_search(worker, str(run.id))
    await session.refresh(run)  # the worker's rollback expires everything on this shared session
    await session.refresh(workspace)
    return run, workspace


async def test_a_finished_scheduled_run_starts_prescreening_with_a_local_model(session, worker, enqueued):
    run, workspace = await _finish_run(session, worker)
    assert (await session.get(WorkspaceSearchRun, run.id)).status == "exhausted"
    assert (workspace.suggest_status, workspace.suggest_run_id) == ("running", run.id)
    assert enqueued == [("suggest_screening", str(workspace.id))]


async def test_a_remote_default_model_says_why_and_starts_nothing(session, worker, enqueued):
    from app.models import LLMConnection  # noqa: PLC0415

    await session.execute(
        update(LLMConnection).values(kind="openai_compatible", base_url="https://api.example.com/v1")
    )
    _, workspace = await _finish_run(session, worker)
    assert enqueued == [] and workspace.suggest_status == "idle"
    assert "api.example.com" in workspace.suggest_error


@pytest.mark.parametrize(
    "kwargs",
    [{"setting": False}, {"scheduled": False}],
)
async def test_other_cases_start_nothing(session, worker, enqueued, kwargs):
    _, workspace = await _finish_run(session, worker, **kwargs)
    assert enqueued == [] and workspace.suggest_run_id is None and workspace.suggest_error is None


async def test_a_busy_workspace_says_the_new_hits_were_not_pre_screened(session, worker, enqueued):
    _, workspace = await _finish_run(session, worker, suggest_status="running")
    assert enqueued == [] and workspace.suggest_run_id is None
    assert workspace.suggest_error == (
        "New hits from a scheduled search weren't pre-screened because another job was running. "
        "Press Suggest to score them."
    )


async def test_no_criteria_says_to_add_some(session, worker, enqueued):
    _, workspace = await _finish_run(session, worker, criteria=None)
    assert enqueued == [] and workspace.suggest_run_id is None
    assert workspace.suggest_error == (
        "New hits from a scheduled search weren't pre-screened: add a research question or inclusion criteria first."
    )


async def test_a_failed_enqueue_puts_the_workspace_back_to_idle(session, worker, monkeypatch):
    async def boom(job, *args):
        raise ConnectionError("redis down")

    monkeypatch.setattr(worker_module.queue, "enqueue", boom)
    run, workspace = await _finish_run(session, worker)
    assert (await session.get(WorkspaceSearchRun, run.id)).status == "exhausted"
    assert (workspace.suggest_status, workspace.suggest_run_id) == ("idle", None)
    assert workspace.suggest_error == "Couldn't start pre-screening after the scheduled search"


async def test_a_stopped_run_does_not_prescreen(session, worker, enqueued):
    from app.models.saved_search import SavedSearch  # noqa: PLC0415

    run = await _new_run(session, "stopped")
    workspace = await session.get(Workspace, run.workspace_id)
    workspace.inclusion_criteria = "RCTs"
    saved = SavedSearch(workspace_id=workspace.id, query="bert", schedule="0 6 * * *", timezone="UTC")
    session.add(saved)
    await session.flush()
    run.saved_search_id = saved.id
    await session.commit()
    await worker_module._mark_exhausted_unless_stopped(session, run)
    assert enqueued == [] and workspace.suggest_status == "idle"


# --- D266: a run stopped by its backstop says so -----------------------------------------------------------


async def test_a_run_hitting_the_wall_clock_deadline_is_cut_short_and_names_unfinished_sources(
    session, worker, monkeypatch
):
    fake_clock = itertools.chain([0.0], itertools.repeat(10_000.0))
    monkeypatch.setattr(worker_module, "_now", lambda: next(fake_clock))
    run = await _new_run(session, "running", ["arxiv", "crossref"])
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    session.add(WorkspaceSearchCursor(run_id=run.id, source="crossref", cursor_json={}, exhausted=True))
    await session.commit()

    await worker_module.run_workspace_search(worker, str(run.id))

    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "exhausted"  # kept for compatibility
    assert reloaded.stats_json["cut_short"] is True
    assert reloaded.stats_json["unfinished_sources"] == ["arxiv"]


async def test_a_run_hitting_the_iteration_cap_is_cut_short_too(session, worker, monkeypatch):
    from app.core import workspace_search as core_module  # noqa: PLC0415

    monkeypatch.setattr(worker_module, "MAX_BATCH_ITERATIONS", 2)

    async def never_ending_page(client, query, page_size, cursor):
        return [], cursor + page_size

    monkeypatch.setitem(core_module._PAGE_FUNCS, "arxiv", never_ending_page)
    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()
    await worker_module.run_workspace_search(worker, str(run.id))
    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert (reloaded.stats_json["cut_short"], reloaded.stats_json["unfinished_sources"]) == (True, ["arxiv"])


async def test_a_run_that_exhausts_every_source_is_not_cut_short(session, worker):
    run = await _new_run(session, "running")
    session.add(WorkspaceSearchCursor(run_id=run.id, source="arxiv", cursor_json={"value": 0}))
    await session.commit()
    await worker_module.run_workspace_search(worker, str(run.id))
    reloaded = await session.get(WorkspaceSearchRun, run.id, populate_existing=True)
    assert reloaded.status == "exhausted" and not reloaded.stats_json.get("cut_short")
    assert "unfinished_sources" not in reloaded.stats_json
