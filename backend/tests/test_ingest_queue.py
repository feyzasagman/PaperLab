"""Papers process one at a time on their own worker, and a paper that keeps stopping the worker ends failed instead of
stuck. Ten ingests at once ran the worker out of memory and left 67 papers mid-ingest (2026-10-13)."""

import pytest
from conftest import FakeArq

from app.core import queue
from app.models import Paper
from app.workers import ingest
from app.workers.settings import IngestWorkerSettings, WorkerSettings

pytestmark = pytest.mark.anyio


def names(functions) -> set[str]:
    return {getattr(f, "name", getattr(f, "__name__", None)) for f in functions}


def test_the_ingest_worker_runs_one_paper_at_a_time_on_its_own_queue():
    assert IngestWorkerSettings.queue_name == queue.INGEST_QUEUE
    assert IngestWorkerSettings.max_jobs == 1
    assert IngestWorkerSettings.max_tries == ingest.MAX_TRIES
    assert names(IngestWorkerSettings.functions) == queue.INGEST_JOBS == {"ingest_paper", "reembed_paper"}


def test_the_main_worker_keeps_the_other_jobs_on_the_default_queue():
    assert getattr(WorkerSettings, "queue_name", None) is None
    assert {"fetch_references", "run_workspace_search", "suggest_screening"} <= names(WorkerSettings.functions)


async def test_core_enqueue_sends_papers_to_the_ingest_queue_and_the_rest_to_the_default(monkeypatch):
    pool = FakeArq()  # asserts each job's queue itself
    monkeypatch.setattr(queue, "_pool", pool)

    await queue.enqueue("ingest_paper", "a")
    await queue.enqueue("reembed_paper", "b")
    await queue.enqueue("run_workspace_search", "c")

    assert pool.jobs == [("ingest_paper", "a"), ("reembed_paper", "b"), ("run_workspace_search", "c")]


async def test_the_fake_pool_refuses_a_paper_job_on_the_default_queue():
    with pytest.raises(AssertionError, match="ingest_paper was queued on None"):
        await FakeArq().enqueue_job("ingest_paper", "a")


@pytest.mark.parametrize("job, args", [("ingest_paper", ("p1",)), ("reembed_paper", ("p1", True))])
async def test_a_paper_job_left_on_the_old_queue_moves_to_the_ingest_queue(job, args):
    """Redis keeps its queue across an update, so a desktop install can hold paper jobs on the default queue."""
    pool = FakeArq()
    forward = next(f for f in WorkerSettings.functions if getattr(f, "name", None) == job)

    await forward.coroutine({"redis": pool}, *args)

    assert pool.jobs == [(job, *args)]


async def test_the_last_try_marks_the_paper_failed_instead_of_leaving_it_mid_ingest(worker_session, sample_pdf, ctx):
    paper = Paper(file_path=str(sample_pdf), title="placeholder", status="embedding")
    worker_session.add(paper)
    await worker_session.commit()

    await ingest.ingest_paper({**ctx, "job_try": ingest.MAX_TRIES}, str(paper.id))
    await worker_session.refresh(paper)

    assert paper.status == "failed"
    assert paper.status_error == ingest.INTERRUPTED.format(times=ingest.MAX_TRIES - 1)
    assert ctx["embedder"].calls == []  # never started again


async def test_a_try_before_the_last_still_ingests(worker_session, sample_pdf, ctx):
    paper = Paper(file_path=str(sample_pdf), title="placeholder")
    worker_session.add(paper)
    await worker_session.commit()

    await ingest.ingest_paper({**ctx, "job_try": ingest.MAX_TRIES - 1}, str(paper.id))
    await worker_session.refresh(paper)

    assert paper.status == "ready"
