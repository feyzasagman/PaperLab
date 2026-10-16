"""M61 enrich_papers: S2 batch answers give library papers their published version. Offline (MockTransport)."""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from sqlalchemy import update

from app.core import hit_enrichment, paper_enrichment, workspaces
from app.models.paper import Paper
from app.providers import semantic_scholar
from app.workers.paper_enrichment import enrich_papers as worker_enrich_papers
from app.workers.settings import WorkerSettings

pytestmark = pytest.mark.anyio

EMNLP = {
    "externalIds": {"DOI": "10.18653/V1/2023.EMNLP-MAIN.1", "ArXiv": "2301.00001"},
    "venue": "Conference on Empirical Methods in Natural Language Processing",
    "journal": {"name": "Proceedings of EMNLP"}, "publicationTypes": ["Conference"],
}
ICML = {
    "externalIds": {"ArXiv": "2301.00002"}, "venue": "International Conference on Machine Learning",
    "journal": {"name": "ArXiv"}, "publicationTypes": ["Conference"],
}
ARXIV_ONLY = {
    "externalIds": {"ArXiv": "2301.00003"}, "venue": "arXiv.org", "journal": {"name": "ArXiv"},
    "publicationTypes": ["JournalArticle"],
}


def uid() -> str:
    return uuid.uuid4().hex[:10]


async def library(session, *papers: Paper):
    ws = await workspaces.create(session, f"Enrich papers {uid()}")
    session.add_all(papers)
    await session.flush()
    for paper in papers:
        await workspaces.add_paper(session, ws.id, paper.id)
    return ws


def paper(doi: str | None, **fields) -> Paper:
    return Paper(title=f"P {uid()}", file_path="/nonexistent.pdf", page_count=5, doi=doi, **fields)


def arxiv_paper(**fields) -> Paper:
    # a unique arXiv id per paper (doi is unique); the S2 answers are positional, so the id never has to match
    return paper(f"10.48550/arxiv.2301.{int(uid(), 16) % 10**6:06d}", **fields)


def s2_client(handler):
    return semantic_scholar.new_client(transport=httpx.MockTransport(handler), retry_429=False)


async def run(session, ws, answers):
    async with s2_client(lambda request: httpx.Response(200, json=answers)) as http:
        return await paper_enrichment.enrich(session, http, ws.id)


async def test_an_arxiv_copy_gets_its_published_version(session):
    p = arxiv_paper()
    ws = await library(session, p)
    out = await run(session, ws, [EMNLP])
    assert (out.looked_up, out.filled, out.not_found, out.remaining) == (1, 1, 0, 0)
    assert p.published_doi == "10.18653/v1/2023.emnlp-main.1"
    assert p.published_venue == "Proceedings of EMNLP"
    assert p.peer_reviewed == "true" and p.published_checked_at is not None


async def test_an_icml_paper_without_a_doi_gets_venue_and_peer_review(session):
    p = arxiv_paper(peer_reviewed="false", source_type="preprint")
    ws = await library(session, p)
    await run(session, ws, [ICML])
    assert p.published_venue == "International Conference on Machine Learning"
    assert p.published_doi is None
    assert p.peer_reviewed == "true"
    assert p.source_type == "preprint"  # kept


async def test_an_arxiv_only_answer_changes_nothing_but_stamps(session):
    p = arxiv_paper()
    ws = await library(session, p)
    out = await run(session, ws, [ARXIV_ONLY])
    assert out.filled == 0
    assert (p.published_doi, p.published_venue, p.peer_reviewed) == (None, None, "unknown")
    assert p.published_checked_at is not None


async def test_manual_fields_are_never_overwritten(session):
    p = arxiv_paper(peer_reviewed="false", manual_fields=["peer_reviewed"])
    ws = await library(session, p)
    await run(session, ws, [EMNLP])
    assert p.peer_reviewed == "false"
    assert p.published_doi == "10.18653/v1/2023.emnlp-main.1" and p.published_venue == "Proceedings of EMNLP"


async def test_every_manual_field_is_kept(session):
    p = arxiv_paper(
        peer_reviewed="false", published_doi="10.1/mine", published_venue="Mine",
        manual_fields=["peer_reviewed", "published_doi", "published_venue"],
    )
    ws = await library(session, p)
    await run(session, ws, [EMNLP])
    assert (p.peer_reviewed, p.published_doi, p.published_venue) == ("false", "10.1/mine", "Mine")


async def test_the_papers_own_doi_is_not_its_published_doi(session):
    p = paper(f"10.1234/own-{uid()}")
    answer = {
        "externalIds": {"DOI": p.doi.upper(), "ArXiv": "2301.00009"}, "journal": {"name": "Nature"},
        "publicationTypes": ["JournalArticle"],
    }
    ws = await library(session, p)
    await run(session, ws, [answer])
    assert p.published_doi is None
    assert p.published_checked_at is not None


async def test_own_doi_venue_and_files_are_untouched(session):
    p = arxiv_paper(venue="My venue")
    doi = p.doi
    ws = await library(session, p)
    await run(session, ws, [EMNLP])
    assert (p.doi, p.venue, p.file_path, p.status) == (doi, "My venue", "/nonexistent.pdf", "uploaded")


async def test_a_second_run_finds_nothing(session):
    p, none = arxiv_paper(), paper(None)
    ws = await library(session, p, none)
    await run(session, ws, [EMNLP])
    assert none.published_checked_at is not None  # no S2 id: never asked, stamped
    assert await paper_enrichment.papers_to_enrich(session, ws.id) == []


async def test_rate_limit_stops_cleanly_and_leaves_the_rest(session, monkeypatch):
    first, second = arxiv_paper(), arxiv_paper()
    ws = await library(session, first, second)
    monkeypatch.setattr(paper_enrichment, "CHUNK", 1)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(hit_enrichment, "sleep", no_sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[EMNLP]) if len(calls) == 1 else httpx.Response(429)

    async with s2_client(handler) as http:
        out = await paper_enrichment.enrich(session, http, ws.id)
    assert out.rate_limited and out.looked_up == 1 and out.remaining == 1
    assert len(await paper_enrichment.papers_to_enrich(session, ws.id)) == 1


async def test_the_worker_job_runs_enrich_and_stores_no_result(session, monkeypatch):
    from app.workers import paper_enrichment as worker

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(worker, "SessionLocal", test_session)
    p = arxiv_paper()
    ws = await library(session, p)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[ICML]))
    await worker.enrich_papers({"transport": transport}, str(ws.id))
    assert p.published_venue == "International Conference on Machine Learning"
    entries = [f for f in WorkerSettings.functions if getattr(f, "coroutine", f) is worker_enrich_papers]
    assert len(entries) == 1 and entries[0].keep_result_s == 0


async def test_enrich_papers_queues_once_and_reports_already_running(session, jobs):
    ws = await library(session, arxiv_paper(), arxiv_paper(), paper(None))
    first = result(await call("enrich_papers", workspace=ws.name))
    second = result(await call("enrich_papers", workspace=ws.name))
    assert first == {"queued": 2, "already_running": False, "s2_off": False}
    assert second == {"queued": 2, "already_running": True, "s2_off": False}
    assert jobs.jobs == [("enrich_papers", str(ws.id))]
    await run(session, ws, [EMNLP, EMNLP])
    assert result(await call("enrich_papers", workspace=ws.name))["queued"] == 0


async def test_enrich_papers_with_s2_off(session, jobs, monkeypatch):
    from app.core import paper_sources
    from app.core.source_registry import SOURCE_IDS

    ws = await library(session, arxiv_paper())
    off = paper_sources.SourceSettings(enabled={**dict.fromkeys(SOURCE_IDS, True), "semantic_scholar": False})

    async def get(_session):
        return off

    monkeypatch.setattr(paper_sources, "get", get)
    out = result(await call("enrich_papers", workspace=ws.name))
    assert out == {"queued": 0, "already_running": False, "s2_off": True}
    assert jobs.jobs == []


async def test_list_tags_counts_a_published_arxiv_copy_as_peer_reviewed(session):
    p = arxiv_paper(source_type="preprint", peer_reviewed="false")
    ws = await library(session, p)
    await call("tag_papers", workspace=ws.name, tag="T", paper_ids=[str(p.id)])
    before = result(await call("list_tags", workspace=ws.name))
    await run(session, ws, [EMNLP])
    after = result(await call("list_tags", workspace=ws.name))
    assert (before[0]["peer_reviewed"], before[0]["preprint"]) == (0, 1)
    assert (after[0]["peer_reviewed"], after[0]["preprint"]) == (1, 0)


# --- fix round 1


async def test_a_hand_edit_made_while_the_batch_runs_is_kept(session, monkeypatch):
    p = arxiv_paper(peer_reviewed="false")
    ws = await library(session, p)
    real = paper_enrichment.batch_with_backoff

    async def edit_then_answer(http, ids):
        await session.execute(
            update(Paper).where(Paper.id == p.id).values(manual_fields=["peer_reviewed", "published_venue"])
            .execution_options(synchronize_session=False)
        )
        return await real(http, ids)

    monkeypatch.setattr(paper_enrichment, "batch_with_backoff", edit_then_answer)
    await run(session, ws, [EMNLP])
    await session.refresh(p)
    assert (p.peer_reviewed, p.published_venue) == ("false", None)
    assert p.published_doi == "10.18653/v1/2023.emnlp-main.1"


async def test_a_paper_that_is_not_a_preprint_gets_no_published_version(session):
    p = paper(f"10.1038/nature-{uid()}", peer_reviewed="false", source_type="journal_article")
    answer = {
        "externalIds": {"DOI": p.doi}, "journal": {"name": "Nature"}, "publicationTypes": ["JournalArticle"],
    }
    ws = await library(session, p)
    await run(session, ws, [answer])
    assert (p.published_venue, p.published_doi, p.peer_reviewed) == (None, None, "false")


async def test_a_normal_paper_still_fills_empty_fields(session):
    p = paper(f"10.1038/nature-{uid()}")
    answer = {"externalIds": {"DOI": p.doi}, "journal": {"name": "Nature"}, "publicationTypes": ["JournalArticle"]}
    ws = await library(session, p)
    await run(session, ws, [answer])
    assert (p.source_type, p.peer_reviewed, p.published_venue) == ("journal_article", "true", None)


async def test_peer_review_is_never_lowered_and_a_preprint_answer_can_still_give_a_doi(session):
    p = arxiv_paper(peer_reviewed="true")
    answer = {
        "externalIds": {"DOI": "10.1234/Pub", "ArXiv": "2301.1"}, "venue": "arXiv.org", "journal": {"name": "ArXiv"},
    }
    ws = await library(session, p)
    await run(session, ws, [answer])
    assert (p.published_doi, p.peer_reviewed) == ("10.1234/pub", "true")
    q = arxiv_paper()
    ws = await library(session, q)
    await run(session, ws, [answer])
    assert (q.published_doi, q.peer_reviewed) == ("10.1234/pub", "unknown")


async def test_a_hand_set_source_type_blocks_peer_review(session):
    p = arxiv_paper(source_type="preprint", manual_fields=["source_type"])
    ws = await library(session, p)
    await run(session, ws, [ICML])
    assert p.peer_reviewed == "unknown" and p.published_venue


async def test_reingest_does_not_flip_a_published_copy_back_to_false(session):
    from app.core import enrichment

    p = arxiv_paper(peer_reviewed="true", published_venue="ICML")
    session.add(p)
    await session.flush()
    await enrichment.fill_source_type(session, p.id, "article", None)
    await session.refresh(p)
    assert p.peer_reviewed == "true"
    plain = arxiv_paper()
    session.add(plain)
    await session.flush()
    await enrichment.fill_source_type(session, plain.id, "article", None)
    await session.refresh(plain)
    assert plain.peer_reviewed == "false"  # unchanged behaviour without a published version


async def test_a_doi_correction_asks_semantic_scholar_again(session):
    from app.core import enrichment

    p = arxiv_paper(published_checked_at=datetime.now(timezone.utc))
    session.add(p)
    await session.flush()
    await enrichment.correct_metadata(session, p.id, {"title": "Retitled"})
    assert p.published_checked_at is not None
    await enrichment.correct_metadata(session, p.id, {"doi": f"10.1234/new-{uid()}"})
    assert p.published_checked_at is None
