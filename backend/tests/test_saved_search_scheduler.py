"""Running due saved searches (M39 F2)."""

import uuid
from datetime import datetime, timezone

import pytest
from conftest import FakeArq
from sqlalchemy import select

from app.core import queue, saved_searches, workspace_search, workspaces
from app.models.workspace_search import WorkspaceSearchRun
from app.workers.settings import IngestWorkerSettings, WorkerSettings

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

UTC = timezone.utc
RUN = uuid.uuid4().hex[:8]
DAILY = "0 2 * * *"
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


@pytest.fixture
def jobs(monkeypatch) -> FakeArq:
    pool = FakeArq()
    monkeypatch.setattr(queue, "_pool", pool)
    return pool


def _async_return(value):
    async def fn(*args, **kwargs):
        return value

    return fn


async def _saved(session, label, now=T0, **fields):
    ws = await workspaces.create(session, f"{label} {RUN}")
    view = await saved_searches.create(session, ws.id, query="llm creativity", schedule=DAILY, timezone="UTC",
                                       sources=["arxiv"], now=now, **fields)
    return ws, view


async def _mine(session, view, started):
    """Only this test's runs: run_due sees every due row in the shared dev DB."""
    ids = set(await session.scalars(select(WorkspaceSearchRun.id).where(WorkspaceSearchRun.saved_search_id == view.id)))
    return [r for r in started if r in ids]


async def _tick(session, view, *at):
    return await _mine(session, view, await saved_searches.run_due(session, datetime(*at, tzinfo=UTC)))


def _enqueued(jobs, runs):
    return [j for j in jobs.jobs if j[1] in {str(r) for r in runs}]


async def test_a_due_search_is_enqueued_exactly_once(session, jobs):
    ws, view = await _saved(session, "Due")
    at = datetime(2026, 10, 14, 2, 0, 30, tzinfo=UTC)

    first = await _mine(session, view, await saved_searches.run_due(session, at))
    again = await _mine(session, view, await saved_searches.run_due(session, at))

    assert len(first) == 1 and again == []
    assert _enqueued(jobs, first) == [("run_workspace_search", str(first[0]))]
    assert len(_enqueued(jobs, first + again)) == 1
    after = await saved_searches.get(session, view.id)
    assert after.next_run_at == datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
    assert after.last_run_id == first[0] and after.last_run_status == "running"


async def test_a_week_of_missed_runs_collapses_to_one(session, jobs):
    ws, view = await _saved(session, "Missed")
    week_later = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)  # 7 fire times missed

    started = await _mine(session, view, await saved_searches.run_due(session, week_later))

    assert len(started) == 1 and len(_enqueued(jobs, started)) == 1
    assert (await saved_searches.get(session, view.id)).next_run_at == datetime(2026, 10, 21, 2, 0, tzinfo=UTC)


async def test_a_stale_claim_loses_the_race(session, jobs, monkeypatch):
    ws, view = await _saved(session, "Race")
    at = datetime(2026, 10, 14, 2, 1, tzinfo=UTC)
    real_due = saved_searches._due

    async def due_twice(s, now):  # the same row seen by two schedulers before either claims it
        rows = await real_due(s, now)
        return rows + rows

    monkeypatch.setattr(saved_searches, "_due", due_twice)
    started = await _mine(session, view, await saved_searches.run_due(session, at))
    assert len(started) == 1 and len(_enqueued(jobs, started)) == 1
    assert (await saved_searches.get(session, view.id)).last_error is None


async def test_disabled_never_runs(session, jobs):
    ws, view = await _saved(session, "Off")
    await saved_searches.update(session, ws.id, view.id, {"enabled": False}, now=T0)
    await saved_searches.run_due(session, datetime(2026, 10, 30, tzinfo=UTC))
    assert await saved_searches._last_run(session, view.id) is None


async def test_a_still_running_previous_run_skips_this_one(session, jobs):
    ws, view = await _saved(session, "Busy")
    [first] = await _tick(session, view, 2026, 10, 14, 2, 1)
    run = await workspace_search.get_run(session, first)
    run.started_at = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)  # the test's clock, not the wall clock
    await session.commit()
    skipped = await _tick(session, view, 2026, 10, 15, 2, 1)
    after = await saved_searches.get(session, view.id)
    assert skipped == [] and len(_enqueued(jobs, [first])) == 1
    assert after.last_error == "skipped: the previous run is still going"
    assert after.next_run_at == datetime(2026, 10, 16, 2, 0, tzinfo=UTC)


async def test_no_source_turned_on_records_an_error_and_keeps_the_schedule(session, jobs, monkeypatch):
    ws, view = await _saved(session, "No source")
    monkeypatch.setattr(saved_searches, "_sources_on", _async_return([]))
    started = await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC))
    assert await _mine(session, view, started) == []
    after = await saved_searches.get(session, view.id)
    assert after.last_error == "no paper source is turned on"
    assert after.next_run_at > datetime(2026, 10, 14, 2, 1, tzinfo=UTC)


async def test_one_broken_search_does_not_stop_the_next(session, jobs, monkeypatch):
    _, bad = await _saved(session, "Broken")
    _, good = await _saved(session, "Fine")
    real_start = saved_searches._start

    async def start(s, row, now):
        if row.id == bad.id:
            raise RuntimeError("boom")
        return await real_start(s, row, now)

    monkeypatch.setattr(saved_searches, "_start", start)
    started = await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC))
    assert len(await _mine(session, good, started)) == 1
    assert (await saved_searches.get(session, bad.id)).last_error == "boom"


async def test_the_run_carries_the_year_range_and_saved_search_id(session, jobs):
    ws, view = await _saved(session, "Years", year_from=2023, year_to=2026)
    [run_id] = await _tick(session, view, 2026, 10, 14, 2, 1)
    run = await workspace_search.get_run(session, run_id)
    assert run.filters_json == {"year_from": 2023, "year_to": 2026} and run.saved_search_id == view.id


async def test_run_now_starts_without_moving_the_schedule(session, jobs):
    ws, view = await _saved(session, "Now")
    run = await saved_searches.run_now(session, ws.id, view.id)
    assert _enqueued(jobs, [run.id]) == [("run_workspace_search", str(run.id))]
    after = await saved_searches.get(session, view.id)
    assert after.next_run_at == view.next_run_at and after.last_run_at is not None  # M6


async def test_a_failed_enqueue_ends_the_run_and_the_next_tick_starts_a_new_one(session, jobs, monkeypatch):
    ws, view = await _saved(session, "Enqueue")
    real = queue.enqueue

    async def broken(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(queue, "enqueue", broken)
    assert await _tick(session, view, 2026, 10, 14, 2, 1) == []
    failed = await saved_searches._last_run(session, view.id)
    assert failed.status == "failed" and failed.stopped_at is not None
    assert (await saved_searches.get(session, view.id)).last_error == "redis is down"

    monkeypatch.setattr(queue, "enqueue", real)
    started = await _tick(session, view, 2026, 10, 15, 2, 1)
    assert len(started) == 1


async def test_a_lost_running_run_older_than_the_stale_limit_is_not_busy(session, jobs):
    ws, view = await _saved(session, "Stale")
    [first] = await _tick(session, view, 2026, 10, 14, 2, 1)
    run = await workspace_search.get_run(session, first)
    run.started_at = datetime(2026, 10, 14, 2, 1, tzinfo=UTC)
    await session.commit()
    later = datetime(2026, 10, 15, 2, 1, tzinfo=UTC)  # 24h on, far past 2 x the job timeout
    started = await _mine(session, view, await saved_searches.run_due(session, later))
    assert len(started) == 1 and started != [first]


async def test_an_error_with_no_message_still_says_what_it_was(session, jobs, monkeypatch):
    ws, view = await _saved(session, "Empty error")

    async def start(s, row, now):
        raise TimeoutError()

    monkeypatch.setattr(saved_searches, "_start", start)
    await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC))
    assert (await saved_searches.get(session, view.id)).last_error == "TimeoutError"


async def test_a_search_deleted_between_due_and_claim_is_skipped(session, jobs, monkeypatch):
    ws, view = await _saved(session, "Gone")
    real_due = saved_searches._due

    async def due_then_delete(s, now):
        rows = await real_due(s, now)
        mine = [r for r in rows if r.id == view.id]
        await saved_searches.delete(s, ws.id, view.id)
        return mine

    monkeypatch.setattr(saved_searches, "_due", due_then_delete)
    assert await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC)) == []


def test_the_scheduler_is_an_every_minute_cron_on_the_main_worker_only():
    [job] = [j for j in WorkerSettings.cron_jobs if j.name == "schedule_saved_searches"]
    assert job.run_at_startup and job.minute is None and job.second == 0
    assert not getattr(IngestWorkerSettings, "cron_jobs", [])
