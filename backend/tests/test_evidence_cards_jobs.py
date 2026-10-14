import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from arq import Retry
from pdf_papers import chunked_paper
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from test_evidence_cards import FULL, Scripted, card_reply
from test_screening_suggest_worker import _LockConnStub

from app.core import evidence_cards, queue, retrieval
from app.core.errors import Conflict
from app.models import EvidenceCard, Workspace, workspace_papers
from app.workers import evidence_cards as worker
from tests.conftest import TEST_DATABASE_URL

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


@pytest.fixture(autouse=True)
def wiring(session, embedder, arq, monkeypatch):
    async def query_embedder(s, given=None):
        return embedder

    @asynccontextmanager
    async def shared_session():
        yield session

    @asynccontextmanager
    async def shared_connection():
        yield _LockConnStub(await session.connection())

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)
    monkeypatch.setattr(queue, "_pool", arq)
    monkeypatch.setattr(worker, "SessionLocal", shared_session)
    monkeypatch.setattr(worker, "engine", SimpleNamespace(connect=shared_connection))


async def make_papers(session, tmp_path, n):
    return [(await chunked_paper(session, tmp_path / str(i), title=f"P{i}"))[0] for i in range(n)]


async def workspace_with(session, papers):
    workspace = Workspace(name=f"Cards {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    for paper in papers:
        await session.execute(insert(workspace_papers).values(workspace_id=workspace.id, paper_id=paper.id))
    return workspace


async def card_of(session, paper):
    await session.flush()
    return await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper.id))


async def test_two_requests_enqueue_one_job(session, tmp_path, arq):
    papers = await make_papers(session, tmp_path, 2)
    ids = [p.id for p in papers]
    first = await evidence_cards.request_cards(session, paper_ids=ids)
    second = await evidence_cards.request_cards(session, paper_ids=ids)
    assert [v.status for v in first] == [v.status for v in second] == ["generating"] * 2
    assert arq.jobs == [("generate_evidence_cards", None, [str(i) for i in ids])]


async def test_ready_cards_are_skipped_unless_regenerating(session, tmp_path, arq):
    (paper,) = await make_papers(session, tmp_path, 1)
    await evidence_cards.build_card(session, paper.id, llm=Scripted(card_reply(**FULL)))
    (view,) = await evidence_cards.request_cards(session, paper_ids=[paper.id])
    assert view.status == "ready" and arq.jobs == []
    (view,) = await evidence_cards.request_cards(session, paper_ids=[paper.id], regenerate=True)
    assert view.status == "generating" and len(arq.jobs) == 1


async def test_an_edited_card_needs_confirm_to_regenerate(session, tmp_path, arq):
    (paper,) = await make_papers(session, tmp_path, 1)
    await evidence_cards.build_card(session, paper.id, llm=Scripted(card_reply(**FULL)))
    await session.execute(update(EvidenceCard).where(EvidenceCard.paper_id == paper.id).values(provenance="llm_edited"))
    with pytest.raises(Conflict) as err:
        await evidence_cards.request_cards(session, paper_ids=[paper.id], regenerate=True)
    assert str(err.value) == "card_edited_needs_confirm" and arq.jobs == []
    (view,) = await evidence_cards.request_cards(session, paper_ids=[paper.id], regenerate=True, confirm=True)
    assert view.status == "generating" and len(arq.jobs) == 1


async def test_the_job_builds_each_paper_in_turn(session, tmp_path):
    papers = await make_papers(session, tmp_path, 2)
    ids = [p.id for p in papers]
    await evidence_cards.request_cards(session, paper_ids=ids)
    llm = Scripted(card_reply(**FULL), card_reply(**FULL))
    await worker.generate_evidence_cards({"llm": llm}, None, [str(i) for i in ids])
    assert len(llm.prompts) == 2
    assert [(await card_of(session, p)).status for p in papers] == ["ready", "ready"]


async def test_a_failing_paper_does_not_stop_the_rest(session, tmp_path, monkeypatch):
    papers = await make_papers(session, tmp_path, 3)
    ids = [p.id for p in papers]
    await evidence_cards.request_cards(session, paper_ids=ids)
    real = evidence_cards.build_card

    async def flaky(s, paper_id, **kw):
        if paper_id == ids[0]:
            raise RuntimeError("boom")
        return await real(s, paper_id, **kw)

    monkeypatch.setattr(evidence_cards, "build_card", flaky)
    await worker.generate_evidence_cards(
        {"llm": Scripted(card_reply(**FULL), card_reply(**FULL))}, None, [str(i) for i in ids]
    )
    # by id: the job's rollback expired `papers`
    cards = [await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == i)) for i in ids]
    assert [(c.status, c.error) for c in cards] == [("failed", "model_error"), ("ready", None), ("ready", None)]


async def test_a_held_lock_makes_the_job_back_off(session, tmp_path):
    papers = await make_papers(session, tmp_path, 1)
    workspace = await workspace_with(session, papers)
    await evidence_cards.request_cards(session, paper_ids=[papers[0].id], workspace_id=workspace.id)
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    key = worker._lock_key(workspace.id, [])
    async with engine.connect() as holder:
        assert await holder.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        try:
            with pytest.raises(Retry):
                await worker.generate_evidence_cards({"llm": Scripted()}, str(workspace.id), [str(papers[0].id)])
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    await engine.dispose()
    assert (await card_of(session, papers[0])).status == "generating"


async def test_a_job_start_fails_old_generating_cards_it_will_not_process(session, tmp_path):
    papers = await make_papers(session, tmp_path, 3)
    workspace = await workspace_with(session, papers)
    old = datetime.now(UTC) - timedelta(seconds=evidence_cards.JOB_TIMEOUT + 60)
    for p in papers[:2]:
        await evidence_cards.request_cards(session, paper_ids=[p.id])
    await session.execute(update(EvidenceCard).where(EvidenceCard.paper_id == papers[0].id).values(updated_at=old))
    # papers[1] is recent: another job may still own it. papers[2] is the one this job runs.
    await evidence_cards.request_cards(session, paper_ids=[papers[2].id], workspace_id=workspace.id)
    await worker.generate_evidence_cards(
        {"llm": Scripted(card_reply(**FULL))}, str(workspace.id), [str(papers[2].id)]
    )
    stale, recent, run = [await card_of(session, p) for p in papers]
    assert (stale.status, stale.error) == ("failed", "interrupted")
    assert recent.status == "generating" and run.status == "ready"


async def test_a_job_near_its_deadline_requeues_the_rest(session, tmp_path, arq, monkeypatch):
    papers = await make_papers(session, tmp_path, 2)
    ids = [str(p.id) for p in papers]
    await evidence_cards.request_cards(session, paper_ids=[p.id for p in papers])
    arq.jobs.clear()
    monkeypatch.setattr(worker, "SAFETY_MARGIN", evidence_cards.JOB_TIMEOUT + 1)  # the deadline has already passed
    await worker.generate_evidence_cards({"llm": Scripted()}, None, ids)
    assert arq.jobs == [("generate_evidence_cards", None, ids)]


async def test_the_last_try_marks_the_cards_busy_instead_of_retrying(session, tmp_path):
    (paper,) = await make_papers(session, tmp_path, 1)
    await evidence_cards.request_cards(session, paper_ids=[paper.id])
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    key = worker._lock_key(None, [paper.id])
    async with engine.connect() as holder:
        assert await holder.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        try:
            await worker.generate_evidence_cards(
                {"llm": Scripted(), "job_try": worker.MAX_TRIES}, None, [str(paper.id)]
            )
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    await engine.dispose()
    card = await card_of(session, paper)
    assert (card.status, card.error) == ("failed", "busy")


async def test_a_stale_generating_card_is_queued_again(session, tmp_path, arq):
    (paper,) = await make_papers(session, tmp_path, 1)
    await evidence_cards.request_cards(session, paper_ids=[paper.id])
    await evidence_cards.request_cards(session, paper_ids=[paper.id])
    assert len(arq.jobs) == 1  # fresh: not queued again
    old = datetime.now(UTC) - timedelta(seconds=evidence_cards.JOB_TIMEOUT + 60)
    await session.execute(update(EvidenceCard).where(EvidenceCard.paper_id == paper.id).values(updated_at=old))
    await evidence_cards.request_cards(session, paper_ids=[paper.id])
    assert len(arq.jobs) == 2


async def test_the_requeue_refreshes_the_waiting_cards(session, tmp_path, monkeypatch):
    papers = await make_papers(session, tmp_path, 1)
    await evidence_cards.request_cards(session, paper_ids=[papers[0].id])
    old = datetime.now(UTC) - timedelta(seconds=evidence_cards.JOB_TIMEOUT + 60)
    await session.execute(update(EvidenceCard).where(EvidenceCard.paper_id == papers[0].id).values(updated_at=old))
    monkeypatch.setattr(worker, "SAFETY_MARGIN", evidence_cards.JOB_TIMEOUT + 1)
    await worker.generate_evidence_cards({"llm": Scripted()}, None, [str(papers[0].id)])
    card = await card_of(session, papers[0])
    assert card.updated_at > old + timedelta(seconds=evidence_cards.JOB_TIMEOUT)


async def test_duplicate_paper_ids_make_one_entry_each(session, tmp_path, arq):
    (paper,) = await make_papers(session, tmp_path, 1)
    views = await evidence_cards.request_cards(session, paper_ids=[paper.id, paper.id])
    assert len(views) == 1
    assert arq.jobs == [("generate_evidence_cards", None, [str(paper.id)])]


async def test_a_failed_enqueue_puts_the_cards_back_to_failed(session, tmp_path, monkeypatch):
    (paper,) = await make_papers(session, tmp_path, 1)

    async def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(queue, "enqueue", boom)
    with pytest.raises(RuntimeError):
        await evidence_cards.request_cards(session, paper_ids=[paper.id])
    card = await card_of(session, paper)
    await session.refresh(card)
    assert (card.status, card.error) == ("failed", "queue_error")
