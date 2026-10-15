import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from conftest import FakeArq
from sqlalchemy import case, delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import discovery, pdf_links, queue, workspace_search
from app.core.paper_sources import SourceSettings
from app.core.screening_suggest import SYSTEM_PROMPT_V2
from app.core.source_registry import SOURCE_IDS
from app.models.references import ExternalRef
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun
from app.providers import discovery_fake
from app.providers.base import LLMError, LLMUnavailable
from app.providers.llm import FakeLLM
from app.workers import screening_suggest as worker_module
from tests.conftest import TEST_DATABASE_URL
from tests.screening_pool import BASE, make_pool

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

POSITIVE = "randomised controlled trial of sleep deprivation in adults"
NEGATIVE = "a survey of graph neural networks for molecules"


def J(decision, reason=None, relevance=3, quotes=()):
    return json.dumps({"relevance": relevance, "decision": decision, "reason": reason, "quotes": list(quotes),
                       "why": "because"})


class ScriptedLLM(FakeLLM):
    """Answers each call from `replies` in order; an Exception instance is raised instead."""

    def __init__(self, replies, on_call=None):
        super().__init__(model="scripted")
        self.replies = list(replies)
        self.on_call = on_call

    async def stream(self, system, prompt):
        self.calls.append((system, prompt))
        if self.on_call:
            await self.on_call(len(self.calls))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield reply


class _LockConnStub:
    """Forwards the lock statements to the test's own pinned connection, but no-ops `commit()`: that connection's
    real transaction boundary belongs to the `session` fixture (rolled back at teardown), and the advisory lock
    functions don't need a commit to take effect anyway."""

    def __init__(self, connection):
        self._connection = connection

    async def scalar(self, *args, **kwargs):
        return await self._connection.scalar(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return await self._connection.execute(*args, **kwargs)

    async def commit(self):
        pass


@pytest.fixture
def worker(session, monkeypatch):
    @asynccontextmanager
    async def shared_session():
        yield session

    @asynccontextmanager
    async def shared_connection():
        yield _LockConnStub(await session.connection())

    monkeypatch.setattr(worker_module, "SessionLocal", shared_session)
    monkeypatch.setattr(worker_module, "engine", SimpleNamespace(connect=shared_connection))

    def use(llm):
        monkeypatch.setattr(worker_module, "build_llm", lambda connection, name, transport=None: llm)
        return llm

    return use


async def _running(session, rows, criteria="RCTs on sleep in adults"):
    workspace, hits = await make_pool(session, rows)
    workspace.inclusion_criteria, workspace.suggest_status = criteria, "running"
    await session.flush()
    return workspace, hits


async def test_worker_suggests_in_ranked_order_and_never_sets_status(session, worker):
    workspace, hits = await _running(session, [
        (POSITIVE, "relevant"), (NEGATIVE, "not_relevant"),
        ("graph neural network benchmark for molecules", None), ("sleep deprivation trial in older adults", None),
    ])
    llm = worker(ScriptedLLM([J("include"), J("exclude", "wrong_topic")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert "sleep deprivation trial in older adults" in llm.calls[0][1]  # ranked first
    assert all(system == SYSTEM_PROMPT_V2 for system, _ in llm.calls)
    for hit in hits:
        await session.refresh(hit)
    assert (hits[3].suggestion, hits[2].suggestion, hits[2].suggestion_reason) == ("include", "exclude", "wrong_topic")
    assert (hits[2].stage1_status, hits[3].stage1_status) == (None, None)
    assert hits[3].suggestion_model is not None and hits[3].suggested_at is not None
    await session.refresh(workspace)
    assert (workspace.suggest_status, workspace.suggest_done) == ("idle", 2)


async def test_worker_skips_hit_decided_mid_job_and_never_sets_status(session, worker):
    workspace, hits = await _running(session, [("a", None), ("b", None)])

    async def decide_second(call):
        if call == 1:
            hits[1].stage1_status = "maybe"
            await session.flush()

    llm = worker(ScriptedLLM([J("include")], on_call=decide_second))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1
    await session.refresh(hits[1])
    assert (hits[1].suggestion, hits[1].stage1_status) == (None, "maybe")


async def test_worker_skips_hit_with_no_text(session, worker):
    workspace, hits = await _running(session, [("", None), ("sleep", None)])
    hits[0].external_ref_id = None
    hits[0].normalized_title = ""
    await session.flush()
    llm = worker(ScriptedLLM([J("include")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1
    await session.refresh(hits[0])
    assert hits[0].suggestion is None


async def test_stop_mid_job_ends_after_the_current_hit(session, worker):
    workspace, hits = await _running(session, [("a", None), ("b", None), ("c", None)])

    async def stop_after_first(call):
        if call == 1:
            workspace.suggest_status = "stopping"
            await session.flush()

    llm = worker(ScriptedLLM([J("include"), J("include"), J("include")], on_call=stop_after_first))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1
    await session.refresh(workspace)
    assert (workspace.suggest_status, workspace.suggest_done) == ("idle", 1)


async def test_a_model_error_on_one_hit_is_unsure_and_the_job_goes_on(session, worker):
    workspace, hits = await _running(session, [("a", None), ("b", None)])
    worker(ScriptedLLM([LLMError("boom"), J("include")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(hits[0])
    await session.refresh(hits[1])
    assert (hits[0].suggestion, hits[0].suggestion_note, hits[0].suggestion_flags) == (
        "unsure", "model error", ["model_error"])
    assert hits[1].suggestion == "include"


async def test_an_unreachable_model_ends_the_job_with_its_message(session, worker):
    workspace, hits = await _running(session, [("a", None), ("b", None)])
    worker(ScriptedLLM([LLMUnavailable("Can't reach localhost:11434")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(workspace)
    assert (workspace.suggest_status, workspace.suggest_error) == ("idle", "Can't reach localhost:11434")
    for hit in hits:
        await session.refresh(hit)
        assert hit.suggestion is None


async def test_resume_only_asks_about_hits_without_a_suggestion(session, worker):
    workspace, hits = await _running(session, [("a", None), ("b", None)])
    hits[0].suggestion = "include"
    await session.flush()
    llm = worker(ScriptedLLM([J("unsure")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1


async def test_suggest_screening_opens_the_lock_connection_exactly_once(session, worker, monkeypatch):
    """The fix holds one dedicated connection (`engine.connect()`) for the lock/unlock pair's entire lifetime,
    specifically so they can never land on two different physical connections (see the test below). This pins
    that mechanism directly and deterministically: a future change that re-opens the connection mid-job — even
    one that happens to still pass the probabilistic test below on a given run — fails this one every time."""
    workspace, hits = await _running(session, [("a", None), ("b", None), ("c", None)])
    worker(ScriptedLLM([J("include")] * 3))

    real_connect = worker_module.engine.connect
    calls = {"n": 0}

    def counting_connect():
        calls["n"] += 1
        return real_connect()

    monkeypatch.setattr(worker_module.engine, "connect", counting_connect)
    await worker_module.suggest_screening({}, str(workspace.id))
    assert calls["n"] == 1


async def test_the_advisory_lock_survives_its_own_session_rotating_connections(monkeypatch):
    """A live-check run on 2026-10-03 found a stuck job: suggest_status froze at "stopping" and the advisory lock
    stayed held forever, blocking every future suggestion run on that workspace until someone manually restarted
    the worker and reset the row. Root cause: the advisory lock is session-level (tied to one physical Postgres
    connection), but the old code took and released it through the ORM `session`, whose own connection can rotate
    to a *different* pooled connection on every `session.commit()` under real concurrent load (e.g. the frontend
    polling GET /screening while the job runs). The lock-holding connection drifted away mid-job and went idle in
    the pool, still holding the lock, forever.

    `test_screening_suggest_worker.py`'s own `session` fixture can't reproduce this: it pins every statement to
    one connection via `AsyncSession(bind=connection, ...)`, so it never rotates. This test instead runs the
    worker against a real small connection pool, with concurrent "noise" queries racing the job's own per-hit
    commits to force genuine rotation — the same way real production traffic does — and then proves the lock is
    actually free afterward by taking it from a separate connection, not just that some statement was executed.

    This one is probabilistic, not deterministic: forcing a specific connection-pool checkout order depends on
    real asyncio/asyncpg scheduling, not on anything this test controls directly. Measured empirically against
    the pre-fix code (a scratch run, not this suite): roughly 25-35% of runs actually hit the leak: under, not
    over, rotation starves the bug of a chance to happen, and over-rotation gives everyone their own connection
    back with no contention either. The test above is the deterministic guard; this one is corroborating
    evidence for the property that actually matters in production, not the sole gate on it."""
    test_engine = create_async_engine(TEST_DATABASE_URL, pool_size=2, max_overflow=3)
    TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

    async with TestSessionLocal() as setup:
        workspace, hits = await make_pool(setup, [(f"paper {i}", None) for i in range(8)])
        workspace.inclusion_criteria, workspace.suggest_status = "RCTs on sleep in adults", "running"
        await setup.commit()
        wid, run_id = workspace.id, hits[0].run_id
        ref_ids = [hit.external_ref_id for hit in hits]

    monkeypatch.setattr(worker_module, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(worker_module, "engine", test_engine, raising=False)
    monkeypatch.setattr(worker_module, "build_llm", lambda *a, **k: ScriptedLLM([J("include")] * len(hits)))

    async def noise():
        for _ in range(40):
            async with TestSessionLocal() as s:
                await s.execute(text("SELECT 1"))
                await s.commit()
            await asyncio.sleep(0)

    try:
        noise_task = asyncio.create_task(noise())
        await worker_module.suggest_screening({}, str(wid))
        await noise_task

        # The probe must come from a connection pool_size's engine can never hand back: advisory locks are
        # re-entrant within one backend, so if `probe` happened to draw the very (leaked) connection that still
        # holds the lock, `pg_try_advisory_lock` would return True for that same session and mask the leak.
        # NullPool guarantees a brand-new physical connection every time, never one `test_engine`'s pool is
        # holding onto.
        key = worker_module._lock_key(wid)
        probe_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with probe_engine.connect() as probe:
                reacquired = await probe.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
                if reacquired:
                    await probe.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await probe.commit()
        finally:
            await probe_engine.dispose()
        assert reacquired is True, "the advisory lock was still held after the job finished"

        async with TestSessionLocal() as check:
            status = await check.scalar(text("SELECT suggest_status FROM workspaces WHERE id = :id"), {"id": wid})
            done = await check.scalar(
                text("SELECT count(*) FROM workspace_search_hits WHERE workspace_id = :id AND suggestion IS NOT NULL"),
                {"id": wid},
            )
        assert (status, done) == ("idle", len(hits))
    finally:
        async with TestSessionLocal() as cleanup:
            await cleanup.execute(delete(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == wid))
            await cleanup.execute(delete(WorkspaceSearchRun).where(WorkspaceSearchRun.id == run_id))
            await cleanup.execute(delete(ExternalRef).where(ExternalRef.id.in_(ref_ids)))
            await cleanup.execute(delete(Workspace).where(Workspace.id == wid))
            await cleanup.commit()
        await test_engine.dispose()


async def test_a_job_for_an_idle_workspace_does_nothing(session, worker):
    workspace, _ = await make_pool(session, [("a", None)])
    llm = worker(ScriptedLLM([]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert llm.calls == []


async def test_worker_stores_the_v2_suggestion_and_never_sets_status(session, worker):
    workspace, hits = await _running(session, [("randomised controlled trial of sleep deprivation in adults", None)])
    worker(ScriptedLLM([J("include", relevance=5, quotes=["trial of sleep deprivation", "not in the abstract"])]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(hits[0])
    hit = hits[0]
    assert (hit.suggestion, hit.suggestion_relevance, hit.suggestion_prompt_version) == ("include", 5, 2)
    assert (hit.suggestion_quotes, hit.suggestion_confidence, hit.suggestion_flags) == (
        ["trial of sleep deprivation"], "low", ["quote_dropped"])
    assert hit.stage1_status is None


async def test_a_hit_with_no_abstract_is_unsure_without_a_model_call(session, worker):
    workspace, hits = await _running(session, [("", None)])
    llm = worker(ScriptedLLM([]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert llm.calls == []
    await session.refresh(hits[0])
    assert (hits[0].suggestion, hits[0].suggestion_flags) == ("unsure", ["no_abstract"])
    assert (hits[0].suggestion_model, hits[0].suggestion_prompt_version) == (None, 2)


async def test_a_run_scoped_job_only_touches_that_runs_hits_and_clears_its_scope(session, worker):
    workspace, hits = await _running(session, [("a", None), ("b", None)])
    other = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q2", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=BASE, stats_json={},
    )
    session.add(other)
    await session.flush()
    hits[1].run_id = other.id
    workspace.suggest_run_id = hits[0].run_id
    await session.flush()
    llm = worker(ScriptedLLM([J("include")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1
    await session.refresh(hits[1])
    await session.refresh(workspace)
    assert hits[1].suggestion is None and workspace.suggest_run_id is None


@pytest.fixture
async def free_and_closed(session, monkeypatch, pdf_dir):
    """A workspace whose pool holds the offline `free` and `closed` papers (a snowball hop lands them), with
    discovery.open_providers answering from the same fake and a FakeArq behind queue.enqueue."""
    workspace, hits = await make_pool(session, [("seed", None)])
    every = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCE_IDS, True))
    providers = discovery.build_providers(every, discovery_fake.transport())

    @asynccontextmanager
    async def open_providers(_session):
        yield providers

    monkeypatch.setattr(discovery, "open_providers", open_providers)
    async def resolve(host):
        return ["93.184.216.34"]  # one public address, as tests/mcp_fixtures.py does

    monkeypatch.setattr(pdf_links, "resolve_host", resolve)
    arq = FakeArq()
    monkeypatch.setattr(queue, "_pool", arq)
    await workspace_search.snowball(session, providers, workspace.id, [], [hits[0].id], backward=True, forward=False)
    rows = (await session.execute(
        select(WorkspaceSearchHit, ExternalRef.doi)
        .join(ExternalRef, WorkspaceSearchHit.external_ref_id == ExternalRef.id)
        .where(WorkspaceSearchHit.workspace_id == workspace.id)
    )).all()
    by_doi = {doi: hit for hit, doi in rows}
    free, closed = by_doi[f"{discovery_fake.DOI_PREFIX}free"], by_doi[f"{discovery_fake.DOI_PREFIX}closed"]
    hits[0].suggestion = "include"
    for hit in (free, closed):
        hit.suggestion, hit.suggestion_relevance = "include", 5  # already suggested: the queue is empty
    # The fake's backward references hold other papers too: suggested without a score, so the queue stays empty.
    await session.execute(
        update(WorkspaceSearchHit)
        .where(WorkspaceSearchHit.workspace_id == workspace.id, WorkspaceSearchHit.suggestion.is_(None))
        .values(suggestion="include")
    )
    workspace.inclusion_criteria, workspace.suggest_status = "x", "running"
    await session.flush()
    yield workspace, free, closed, arq
    await providers.aclose()


async def test_auto_import_is_off_by_default(session, worker, free_and_closed):
    workspace, free, closed, arq = free_and_closed
    worker(ScriptedLLM([]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(free)
    assert free.acquisition_status == "not_attempted" and arq.jobs == []


async def test_auto_import_takes_the_free_pdf_hits_rated_at_the_bar(session, worker, free_and_closed):
    workspace, free, closed, arq = free_and_closed
    workspace.auto_import_min_relevance = 4
    await session.flush()
    worker(ScriptedLLM([]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(free)
    await session.refresh(closed)
    assert (free.acquisition_status, closed.acquisition_status) == ("imported", "failed")
    assert arq.jobs == [("ingest_paper", str(free.paper_id))]
    assert free.stage1_status is None


async def test_auto_import_skips_a_decided_hit_and_a_rating_below_the_bar(session, worker, free_and_closed):
    workspace, free, closed, arq = free_and_closed
    workspace.auto_import_min_relevance = 5
    free.suggestion_relevance = 4
    closed.stage1_status = "relevant"
    await session.flush()
    worker(ScriptedLLM([]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert arq.jobs == []


async def test_auto_import_skips_a_hit_the_model_said_to_exclude(session, worker, free_and_closed):
    workspace, free, closed, arq = free_and_closed
    workspace.auto_import_min_relevance = 4
    free.suggestion, free.suggestion_reason = "exclude", "wrong_topic"  # rated 5, but an exclude
    await session.flush()
    worker(ScriptedLLM([]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(free)
    assert free.acquisition_status == "not_attempted" and arq.jobs == []


async def _stop_during_the_only_hit(session, worker, free_and_closed):
    workspace, free, closed, arq = free_and_closed
    workspace.auto_import_min_relevance = 4
    # Every hit but the seed already has a suggestion: one model call, and Stop is pressed during it.
    await session.execute(
        update(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == workspace.id)
        .values(suggestion=case((WorkspaceSearchHit.normalized_title == "paper 0", None), else_="include"))
    )
    await session.flush()

    async def stop(call):
        workspace.suggest_status = "stopping"
        await session.flush()

    llm = worker(ScriptedLLM([J("include")], on_call=stop))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1
    return arq


async def test_stop_pressed_during_the_last_hit_does_not_auto_import(session, worker, free_and_closed):
    arq = await _stop_during_the_only_hit(session, worker, free_and_closed)
    assert arq.jobs == []


async def test_a_stopped_job_does_not_auto_import(session, worker, free_and_closed):
    workspace, free, closed, arq = free_and_closed
    arq = await _stop_during_the_only_hit(session, worker, free_and_closed)
    await session.refresh(free)
    assert (free.acquisition_status, arq.jobs) == ("not_attempted", [])


async def test_a_capped_start_scores_only_the_best_suggest_total_hits(session, worker):  # D265
    workspace, hits = await _running(session, [(POSITIVE, "relevant"), (POSITIVE + " again", None), (NEGATIVE, None)])
    workspace.suggest_total = 1
    llm = worker(ScriptedLLM([J("include")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    assert len(llm.calls) == 1


async def _run_until_clock_passes_deadline(session, worker, monkeypatch, *, stop=False):
    workspace, _ = await _running(session, [("a", None), ("b", None), ("c", None)])
    workspace.suggest_total = 3
    await session.flush()
    now = [0.0]
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def after_first(call):
        if stop:
            workspace.suggest_status = "stopping"
            await session.flush()
        else:
            now[0] = 10**9

    worker(ScriptedLLM([J("include")] * 3, on_call=after_first))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(workspace)
    return workspace


async def test_a_job_that_hits_its_time_limit_says_so_with_done_and_total(session, worker, monkeypatch):
    workspace = await _run_until_clock_passes_deadline(session, worker, monkeypatch)
    assert workspace.suggest_done == 1
    assert "4-hour limit after 1 of 3" in workspace.suggest_error
    assert "start pre-screening again" in workspace.suggest_error


async def test_a_stop_leaves_no_time_limit_error(session, worker, monkeypatch):
    workspace = await _run_until_clock_passes_deadline(session, worker, monkeypatch, stop=True)
    assert workspace.suggest_error is None


async def test_a_normal_finish_leaves_no_error(session, worker):
    workspace, _ = await _running(session, [("a", None), ("b", None)])
    worker(ScriptedLLM([J("include"), J("include")]))
    await worker_module.suggest_screening({}, str(workspace.id))
    await session.refresh(workspace)
    assert workspace.suggest_error is None
