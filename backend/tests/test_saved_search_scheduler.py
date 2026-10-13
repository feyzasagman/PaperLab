"""Running due saved searches (M39 F2)."""

import uuid
from datetime import datetime, timezone

import pytest
from conftest import FakeArq

from app.core import queue, saved_searches, workspace_search, workspaces
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


async def test_a_due_search_is_enqueued_exactly_once(session, jobs):
    ws, view = await _saved(session, "Due")
    at = datetime(2026, 10, 14, 2, 0, 30, tzinfo=UTC)

    first = await saved_searches.run_due(session, at)
    again = await saved_searches.run_due(session, at)

    assert len(first) == 1 and again == []
    assert jobs.jobs == [("run_workspace_search", str(first[0]))]
    after = await saved_searches.get(session, view.id)
    assert after.next_run_at == datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
    assert after.last_run_id == first[0] and after.last_run_status == "running"


async def test_a_week_of_missed_runs_collapses_to_one(session, jobs):
    ws, view = await _saved(session, "Missed")
    week_later = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)  # 7 fire times missed

    started = await saved_searches.run_due(session, week_later)

    assert len(started) == 1 and len(jobs.jobs) == 1
    assert (await saved_searches.get(session, view.id)).next_run_at == datetime(2026, 10, 21, 2, 0, tzinfo=UTC)


async def test_a_stale_claim_loses_the_race(session, jobs, monkeypatch):
    ws, view = await _saved(session, "Race")
    at = datetime(2026, 10, 14, 2, 1, tzinfo=UTC)
    real_due = saved_searches._due

    async def due_twice(s, now):  # the same row seen by two schedulers before either claims it
        rows = await real_due(s, now)
        return rows + rows

    monkeypatch.setattr(saved_searches, "_due", due_twice)
    started = await saved_searches.run_due(session, at)
    assert len(started) == 1 and len(jobs.jobs) == 1
    assert (await saved_searches.get(session, view.id)).last_error is None


async def test_disabled_never_runs(session, jobs):
    ws, view = await _saved(session, "Off")
    await saved_searches.update(session, ws.id, view.id, {"enabled": False}, now=T0)
    assert await saved_searches.run_due(session, datetime(2026, 10, 30, tzinfo=UTC)) == []


async def test_a_still_running_previous_run_skips_this_one(session, jobs):
    ws, view = await _saved(session, "Busy")
    [first] = await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC))
    skipped = await saved_searches.run_due(session, datetime(2026, 10, 15, 2, 1, tzinfo=UTC))
    after = await saved_searches.get(session, view.id)
    assert skipped == [] and len(jobs.jobs) == 1
    assert after.last_error == "skipped: the previous run is still going"
    assert after.next_run_at == datetime(2026, 10, 16, 2, 0, tzinfo=UTC)


async def test_no_source_turned_on_records_an_error_and_keeps_the_schedule(session, jobs, monkeypatch):
    ws, view = await _saved(session, "No source")
    monkeypatch.setattr(saved_searches, "_sources_on", _async_return([]))
    assert await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC)) == []
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
    assert len(started) == 1
    assert (await saved_searches.get(session, bad.id)).last_error == "boom"


async def test_the_run_carries_the_year_range_and_saved_search_id(session, jobs):
    ws, view = await _saved(session, "Years", year_from=2023, year_to=2026)
    [run_id] = await saved_searches.run_due(session, datetime(2026, 10, 14, 2, 1, tzinfo=UTC))
    run = await workspace_search.get_run(session, run_id)
    assert run.filters_json == {"year_from": 2023, "year_to": 2026} and run.saved_search_id == view.id


async def test_run_now_starts_without_moving_the_schedule(session, jobs):
    ws, view = await _saved(session, "Now")
    run = await saved_searches.run_now(session, ws.id, view.id)
    assert jobs.jobs == [("run_workspace_search", str(run.id))]
    assert (await saved_searches.get(session, view.id)).next_run_at == view.next_run_at


def test_the_scheduler_is_an_every_minute_cron_on_the_main_worker_only():
    [job] = [j for j in WorkerSettings.cron_jobs if j.name == "schedule_saved_searches"]
    assert job.run_at_startup and job.minute is None and job.second == 0
    assert not getattr(IngestWorkerSettings, "cron_jobs", [])
