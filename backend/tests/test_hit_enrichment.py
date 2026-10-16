"""M59 enrich_hits: S2 batch answers fill triage data and missing abstracts on older hits. Offline (MockTransport)."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result

from app.core import hit_enrichment
from app.models.references import ExternalRef
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun
from app.providers import semantic_scholar
from app.workers.hit_enrichment import enrich_hits as worker_enrich_hits
from app.workers.settings import WorkerSettings

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
JOURNAL = {"externalIds": {"DOI": "10.1/x"}, "journal": {"name": "Cognition"}, "publicationTypes": ["JournalArticle"],
           "abstract": "From S2."}


async def pool(session, refs: list[ExternalRef]):
    workspace = Workspace(name=f"Enrich {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={}, sources_json=["arxiv"],
        status="exhausted", started_at=NOW, stats_json={},
    )
    session.add(run)
    session.add_all(refs)
    await session.flush()
    for ref in refs:
        session.add(WorkspaceSearchHit(
            workspace_id=workspace.id, run_id=run.id, external_ref_id=ref.id, source_method="database_search",
            normalized_title=ref.title, first_seen_at=NOW,
        ))
    await session.flush()
    return workspace, run


def ref(title: str, **fields) -> ExternalRef:
    return ExternalRef(title=title, **fields)


def s2_client(handler):
    return semantic_scholar.new_client(transport=httpx.MockTransport(handler), retry_429=False)


@pytest.fixture
def waits(monkeypatch):
    recorded: list[float] = []

    async def record(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(hit_enrichment, "sleep", record)
    return recorded


async def test_batch_posts_ids_and_aligns_nulls():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[JOURNAL, None])

    async with s2_client(handler) as http:
        answers = await semantic_scholar.batch(http, ["DOI:10.1/x", "DOI:10.1/missing"])
    assert answers == [JOURNAL, None]
    [request] = seen
    assert request.method == "POST" and request.url.params["fields"] == semantic_scholar.BATCH_FIELDS
    assert request.read() == b'{"ids":["DOI:10.1/x","DOI:10.1/missing"]}'
    assert request.extensions["timeout"]["read"] == 60.0  # a 500-id batch with abstracts outlasts the 10 s default


async def test_enrich_fills_only_empty_fields_and_counts(session):
    fresh = ref("fresh", doi="10.1/x")
    typed = ref("typed", doi="10.1/y", source_type="preprint", abstract="Mine.")
    gone = ref("gone", doi="10.1/gone")
    no_ids = ref("no ids")
    ws, _ = await pool(session, [fresh, typed, gone, no_ids])
    assert {r.title for r in await hit_enrichment.refs_to_enrich(session, ws.id, None)} == {"fresh", "gone"}
    typed.abstract = ""  # now it lacks an abstract, but keeps its source_type
    await session.flush()
    answers = {"DOI:10.1/x": JOURNAL, "DOI:10.1/y": {**JOURNAL, "abstract": "S2 text."}, "DOI:10.1/gone": None}
    sent = []

    def handler(request):
        asked = json.loads(request.read())["ids"]
        sent.append(asked)
        return httpx.Response(200, json=[answers[i] for i in asked])

    async with s2_client(handler) as http:
        outcome = await hit_enrichment.enrich(session, http, ws.id, None)

    assert len(sent) == 1 and sorted(sent[0]) == ["DOI:10.1/gone", "DOI:10.1/x", "DOI:10.1/y"]
    assert (outcome.looked_up, outcome.filled, outcome.not_found) == (3, 2, 1)
    assert (outcome.remaining, outcome.rate_limited) == (0, False)
    assert (fresh.source_type, fresh.published_venue, fresh.abstract) == ("journal_article", "Cognition", "From S2.")
    assert (typed.source_type, typed.abstract) == ("preprint", "S2 text.")
    assert gone.source_type is None


async def test_an_existing_abstract_and_triage_are_never_overwritten(session):
    kept = ref("kept", doi="10.1/x", source_type="preprint", abstract="Mine.")
    other = ref("other", doi="10.1/z")
    ws, _ = await pool(session, [kept, other])
    kept.source_type = None  # picked for its missing source_type, abstract stays
    await session.flush()
    async with s2_client(lambda request: httpx.Response(200, json=[JOURNAL, JOURNAL])) as http:
        await hit_enrichment.enrich(session, http, ws.id, None)
    assert kept.abstract == "Mine."


async def test_s2_id_prefers_a_doi_and_skips_arxiv_dois():
    assert hit_enrichment.s2_id(ref("a", doi="10.1/x", external_ids={"arxiv": "2301.01234"})) == "DOI:10.1/x"
    assert hit_enrichment.s2_id(ref("b", external_ids={"arxiv": "2301.01234"})) == "ARXIV:2301.01234"
    assert hit_enrichment.s2_id(ref("c", doi="10.48550/arxiv.2301.01234", external_ids={"arxiv": "2301.01234"})) == (
        "ARXIV:2301.01234"
    )
    assert hit_enrichment.s2_id(ref("d")) is None


async def test_a_429_then_success_is_retried(session, waits):
    one = ref("one", doi="10.1/x")
    ws, _ = await pool(session, [one])
    answers = iter([httpx.Response(429), httpx.Response(200, json=[JOURNAL])])
    async with s2_client(lambda request: next(answers)) as http:
        outcome = await hit_enrichment.enrich(session, http, ws.id, None)
    assert waits == [2] and outcome.filled == 1 and not outcome.rate_limited


async def test_retry_after_is_honoured_and_capped(session, waits):
    one = ref("one", doi="10.1/x")
    ws, _ = await pool(session, [one])
    answers = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "900"}),
        httpx.Response(200, json=[JOURNAL]),
    ])
    async with s2_client(lambda request: next(answers)) as http:
        outcome = await hit_enrichment.enrich(session, http, ws.id, None)
    assert waits == [7, 60] and outcome.filled == 1


async def test_four_429s_stop_the_job_cleanly(session, waits):
    ws, _ = await pool(session, [ref("a", doi="10.1/a"), ref("b", doi="10.1/b")])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    async with s2_client(handler) as http:
        outcome = await hit_enrichment.enrich(session, http, ws.id, None)
    assert len(calls) == 4 and waits == [2, 5, 15]
    assert (outcome.rate_limited, outcome.remaining, outcome.filled) == (True, 2, 0)
    assert len(await hit_enrichment.refs_to_enrich(session, ws.id, None)) == 2  # still unasked, still queued


async def test_a_failing_chunk_does_not_stop_the_job(session, monkeypatch):
    monkeypatch.setattr(hit_enrichment, "CHUNK", 1)
    refs = [ref(f"r{n}", doi=f"10.1/{n}") for n in range(3)]
    ws, _ = await pool(session, refs)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500) if len(calls) == 1 else httpx.Response(200, json=[JOURNAL])

    async with s2_client(handler) as http:
        outcome = await hit_enrichment.enrich(session, http, ws.id, None)
    assert len(calls) == 3
    assert (outcome.looked_up, outcome.filled, outcome.not_found, outcome.remaining) == (2, 2, 1, 0)


async def test_enrich_hits_tool_queues_once_with_the_count(session, jobs):
    done = ref("done", doi="10.1/c", source_type="preprint", abstract="Has one.")
    ws, run = await pool(session, [ref("a", doi="10.1/a"), ref("b", doi="10.1/b"), done])
    out = result(await call("enrich_hits", workspace=ws.name))
    assert out == {"queued": 2, "already_running": False}
    assert jobs.jobs == [("enrich_hits", str(ws.id), None)]
    result(await call("enrich_hits", workspace=ws.name, run_id=str(run.id)))
    assert jobs.jobs[-1] == ("enrich_hits", str(ws.id), str(run.id))


async def test_a_second_enrich_hits_while_a_job_runs_queues_nothing(session, jobs):
    ws, _ = await pool(session, [ref("a", doi="10.1/a")])
    first = result(await call("enrich_hits", workspace=ws.name))
    second = result(await call("enrich_hits", workspace=ws.name))
    assert first == {"queued": 1, "already_running": False}
    assert second == {"queued": 1, "already_running": True}
    assert jobs.jobs == [("enrich_hits", str(ws.id), None)]


async def test_enrich_hits_with_nothing_to_do_queues_nothing(session, jobs):
    ws, _ = await pool(session, [ref("done", doi="10.1/c", source_type="preprint", abstract="Has one.")])
    assert result(await call("enrich_hits", workspace=ws.name)) == {"queued": 0, "already_running": False}
    assert jobs.jobs == []


async def test_the_worker_job_runs_enrich(session, monkeypatch):
    from app.workers import hit_enrichment as worker

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(worker, "SessionLocal", test_session)
    one = ref("one", doi="10.1/x")
    ws, _ = await pool(session, [one])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[JOURNAL]))

    await worker.enrich_hits({"transport": transport}, str(ws.id))

    assert one.source_type == "journal_article"


async def test_a_second_enrich_after_a_full_run_finds_nothing(session):
    refs = [ref("filled", doi="10.1/x"), ref("unknown", doi="10.1/gone"), ref("no id", doi="10.48550/arxiv.2301.1")]
    ws, _ = await pool(session, refs)
    async with s2_client(lambda request: httpx.Response(200, json=[JOURNAL, None])) as http:
        await hit_enrichment.enrich(session, http, ws.id, None)
    assert await hit_enrichment.refs_to_enrich(session, ws.id, None) == []
    assert all(r.triage_checked_at is not None for r in refs)


async def test_a_short_answer_fails_the_chunk_and_leaves_refs_unchecked(session):
    refs = [ref("a", doi="10.1/a"), ref("b", doi="10.1/b")]
    ws, _ = await pool(session, refs)
    async with s2_client(lambda request: httpx.Response(200, json=[JOURNAL])) as http:
        outcome = await hit_enrichment.enrich(session, http, ws.id, None)
    assert (outcome.looked_up, outcome.filled, outcome.not_found) == (0, 0, 2)
    assert len(await hit_enrichment.refs_to_enrich(session, ws.id, None)) == 2


async def test_enrich_hits_is_refused_when_semantic_scholar_is_off(session, jobs, monkeypatch):
    from app.core import paper_sources
    from app.core.source_registry import SOURCE_IDS

    ws, _ = await pool(session, [ref("a", doi="10.1/a")])
    off = paper_sources.SourceSettings(enabled={**dict.fromkeys(SOURCE_IDS, True), "semantic_scholar": False})

    async def get(_session):
        return off

    monkeypatch.setattr(paper_sources, "get", get)
    response = await call("enrich_hits", workspace=ws.name)
    assert response.is_error and response.structured_content["error"] == "source_off"
    assert jobs.jobs == []


async def test_enrich_hits_job_stores_no_result():
    # A stored result keeps the job id taken for an hour, so a re-run after a rate-limited stop is refused.
    entries = [f for f in WorkerSettings.functions if getattr(f, "coroutine", f) is worker_enrich_hits]
    assert len(entries) == 1 and entries[0].keep_result_s == 0
