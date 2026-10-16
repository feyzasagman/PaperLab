"""M59 enrich_hits: S2 batch answers fill triage data and missing abstracts on older hits. Offline (MockTransport)."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from sqlalchemy import select

from app.core import hit_enrichment
from app.models.references import ExternalRef
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun
from app.providers import arxiv, semantic_scholar
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
    assert out == {"queued": 2, "already_running": False, "suggestions_cleared": 0, "s2_off": False}
    assert jobs.jobs == [("enrich_hits", str(ws.id), None)]
    result(await call("enrich_hits", workspace=ws.name, run_id=str(run.id)))
    assert jobs.jobs[-1] == ("enrich_hits", str(ws.id), str(run.id))


async def test_a_second_enrich_hits_while_a_job_runs_queues_nothing(session, jobs):
    ws, _ = await pool(session, [ref("a", doi="10.1/a")])
    first = result(await call("enrich_hits", workspace=ws.name))
    second = result(await call("enrich_hits", workspace=ws.name))
    assert first == {"queued": 1, "already_running": False, "suggestions_cleared": 0, "s2_off": False}
    assert second == {"queued": 1, "already_running": True, "suggestions_cleared": 0, "s2_off": False}
    assert jobs.jobs == [("enrich_hits", str(ws.id), None)]


async def test_enrich_hits_with_nothing_to_do_queues_nothing(session, jobs):
    ws, _ = await pool(session, [ref("done", doi="10.1/c", source_type="preprint", abstract="Has one.")])
    out = result(await call("enrich_hits", workspace=ws.name))
    assert out == {"queued": 0, "already_running": False, "suggestions_cleared": 0, "s2_off": False}
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


async def test_enrich_hits_queues_nothing_when_semantic_scholar_is_off(session, jobs, monkeypatch):
    from app.core import paper_sources
    from app.core.source_registry import SOURCE_IDS

    ws, _ = await pool(session, [ref("a", doi="10.1/a")])
    off = paper_sources.SourceSettings(enabled={**dict.fromkeys(SOURCE_IDS, True), "semantic_scholar": False})

    async def get(_session):
        return off

    monkeypatch.setattr(paper_sources, "get", get)
    done = ref("done", abstract="Has one.")
    other, _ = await pool(session, [done])
    suggest(await hit_of(session, done), ["no_abstract"])
    await session.flush()
    assert result(await call("enrich_hits", workspace=other.name)) == {
        "queued": 0, "already_running": False, "suggestions_cleared": 1, "s2_off": True,
    }
    assert result(await call("enrich_hits", workspace=ws.name))["s2_off"] is True
    assert jobs.jobs == []


async def test_enrich_hits_job_stores_no_result():
    # A stored result keeps the job id taken for an hour, so a re-run after a rate-limited stop is refused.
    entries = [f for f in WorkerSettings.functions if getattr(f, "coroutine", f) is worker_enrich_hits]
    assert len(entries) == 1 and entries[0].keep_result_s == 0


async def hit_of(session, r: ExternalRef) -> WorkspaceSearchHit:
    hit = (await session.execute(
        select(WorkspaceSearchHit).where(WorkspaceSearchHit.external_ref_id == r.id)
    )).scalar_one()
    await session.refresh(hit)
    return hit


def suggest(hit: WorkspaceSearchHit, flags: list[str], status: str | None = None) -> None:
    hit.suggestion, hit.suggestion_reason, hit.suggestion_note = "unsure", None, "n"
    hit.suggestion_model, hit.suggested_at, hit.suggestion_relevance = "m", NOW, 3
    hit.suggestion_quotes, hit.suggestion_confidence = ["q"], "low"
    hit.suggestion_flags, hit.suggestion_prompt_version = flags, 2
    hit.stage1_status = status


async def test_stale_no_abstract_suggestions_are_cleared_for_undecided_hits_only(session):
    stale = ref("stale", abstract="Now has one.")
    decided = ref("decided", abstract="Has one.")
    verdict = ref("verdict", abstract="Has one.")
    still_empty = ref("still empty", abstract="  ")
    ws, _ = await pool(session, [stale, decided, verdict, still_empty])
    hits = {r: await hit_of(session, r) for r in (stale, decided, verdict, still_empty)}
    suggest(hits[stale], ["no_abstract"])
    suggest(hits[decided], ["no_abstract"], status="relevant")
    suggest(hits[verdict], [])
    suggest(hits[still_empty], ["no_abstract"])
    await session.flush()

    assert await hit_enrichment.clear_stale_no_abstract(session, ws.id) == 1

    cleared = await hit_of(session, stale)
    assert (cleared.suggestion, cleared.suggestion_reason, cleared.suggestion_note, cleared.suggestion_model) == (
        None, None, None, None)
    assert (cleared.suggested_at, cleared.suggestion_relevance, cleared.suggestion_confidence) == (None, None, None)
    assert (cleared.suggestion_quotes, cleared.suggestion_flags, cleared.suggestion_prompt_version) == (
        None, None, None)
    kept = await hit_of(session, decided)
    assert (kept.suggestion, kept.stage1_status, kept.suggestion_flags) == ("unsure", "relevant", ["no_abstract"])
    assert (await hit_of(session, verdict)).suggestion == "unsure"
    assert (await hit_of(session, still_empty)).suggestion_flags == ["no_abstract"]


async def test_enrich_hits_clears_even_with_nothing_to_enrich(session, jobs):
    done = ref("done", doi="10.1/c", source_type="preprint", abstract="Has one.")
    ws, _ = await pool(session, [done])
    suggest(await hit_of(session, done), ["no_abstract"])
    await session.flush()
    out = result(await call("enrich_hits", workspace=ws.name))
    assert out == {"queued": 0, "already_running": False, "suggestions_cleared": 1, "s2_off": False}
    assert jobs.jobs == []
    assert (await hit_of(session, done)).suggestion is None


async def test_the_job_clears_after_filling_abstracts(session, monkeypatch):
    from app.workers import hit_enrichment as worker

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(worker, "SessionLocal", test_session)
    one = ref("one", doi="10.1/x")
    ws, _ = await pool(session, [one])
    suggest(await hit_of(session, one), ["no_abstract"])
    await session.flush()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[JOURNAL]))

    await worker.enrich_hits({"transport": transport}, str(ws.id))

    assert one.abstract == "From S2."
    hit = await hit_of(session, one)
    assert hit.suggestion is None and hit.suggestion_flags is None


async def test_apply_s2_keeps_the_arxiv_id():
    plain = ref("plain", doi="10.1/x")
    assert not plain.external_ids
    hit_enrichment.apply_s2(plain, {**JOURNAL, "externalIds": {"DOI": "10.1/x", "ArXiv": "2301.01234"}})
    assert plain.external_ids == {"arxiv": "2301.01234"}
    own = ref("own", doi="10.1/y", external_ids={"arxiv": "2000.00001", "x": "1"})
    before = own.external_ids
    hit_enrichment.apply_s2(own, {**JOURNAL, "externalIds": {"ArXiv": "2301.01234"}})
    assert own.external_ids == {"arxiv": "2000.00001", "x": "1"} and own.external_ids is before


def atom(ids: list[str], skip: tuple[str, ...] = ()) -> str:
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/{i}v1</id><title>t</title><summary>Abstract of {i}.</summary></entry>"
        for i in ids if i not in skip
    )
    return f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">{entries}</feed>'


def arxiv_refs(count: int, **fields) -> list[ExternalRef]:
    return [ref(f"a{n}", external_ids={"arxiv": f"2301.{n:05d}"}, **fields) for n in range(count)]


def arxiv_client(handler):
    return arxiv.new_client(transport=httpx.MockTransport(handler))


def id_list(request) -> list[str]:
    return request.url.params["id_list"].split(",")


async def test_arxiv_fallback_fills_only_empty_abstracts_and_paces_requests(session, waits):
    refs = arxiv_refs(150)
    has_one = ref("has one", abstract="Mine.", external_ids={"arxiv": "2301.99999"})
    no_arxiv = ref("no arxiv", doi="10.1/n")
    ws, _ = await pool(session, [*refs, has_one, no_arxiv])
    sent = []

    def handler(request):
        sent.append(id_list(request))
        return httpx.Response(200, text=atom(id_list(request)))

    async with arxiv_client(handler) as http:
        outcome = await hit_enrichment.arxiv_abstracts(session, http, [*refs, has_one, no_arxiv])

    assert [len(ids) for ids in sent] == [100, 50] and waits == [3.0]
    assert (outcome.asked, outcome.filled, outcome.rate_limited) == (150, 150, False)
    assert refs[0].abstract == "Abstract of 2301.00000." and has_one.abstract == "Mine." and no_arxiv.abstract is None


async def test_arxiv_429_then_success_and_four_429s_stop(session, monkeypatch, waits):
    monkeypatch.setattr(hit_enrichment, "ARXIV_CHUNK", 2)
    refs = arxiv_refs(4)
    ws, _ = await pool(session, refs)
    answers = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, text=atom(["2301.00000"])),  # arXiv answers one of the two ids
    ])
    async with arxiv_client(lambda request: next(answers)) as http:
        outcome = await hit_enrichment.arxiv_abstracts(session, http, refs[:2])
    assert waits == [7] and (outcome.asked, outcome.filled, outcome.rate_limited) == (2, 1, False)
    assert refs[0].abstract and refs[1].abstract is None

    waits.clear()
    calls = []

    def busy(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) % 2 else 429)

    async with arxiv_client(busy) as http:
        outcome = await hit_enrichment.arxiv_abstracts(session, http, refs[1:])
    assert len(calls) == 4 and waits == [2, 5, 15]  # stopped after the 4th in a row; the rest is left
    assert outcome.rate_limited and outcome.filled == 0 and refs[2].abstract is None


async def test_job_skips_arxiv_after_an_s2_rate_limit(session, monkeypatch):
    from app.workers import hit_enrichment as worker

    @asynccontextmanager
    async def test_session():
        yield session

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(worker, "SessionLocal", test_session)
    monkeypatch.setattr(hit_enrichment, "sleep", no_sleep)
    one = ref("one", external_ids={"arxiv": "2301.00001"})
    ws, _ = await pool(session, [one])
    arxiv_calls = []

    def arxiv_handler(request):
        arxiv_calls.append(request)
        return httpx.Response(200, text=atom(id_list(request)))

    ctx = {"transport": httpx.MockTransport(lambda request: httpx.Response(429)),
           "arxiv_transport": httpx.MockTransport(arxiv_handler)}
    await worker.enrich_hits(ctx, str(ws.id))
    assert arxiv_calls == [] and one.abstract is None

    # S2 answers but has no abstract: the job falls back to arXiv
    ctx["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=[{**JOURNAL, "abstract": None}]))
    await hit_enrichment_reset(session, one)
    await worker.enrich_hits(ctx, str(ws.id))
    assert len(arxiv_calls) == 1 and one.abstract == "Abstract of 2301.00001."


async def hit_enrichment_reset(session, r):
    r.triage_checked_at = None
    await session.flush()


async def test_arxiv_gaps_are_queued_again_after_a_stop(session, jobs, monkeypatch, waits):
    from app.workers import hit_enrichment as worker

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(worker, "SessionLocal", test_session)
    stamped = [ref(f"s{n}", external_ids={"arxiv": f"2301.{n:05d}"}, triage_checked_at=NOW) for n in range(2)]
    ws, _ = await pool(session, stamped)  # S2 already asked about both, neither has an abstract
    assert result(await call("enrich_hits", workspace=ws.name))["queued"] == 2
    s2 = httpx.MockTransport(lambda request: httpx.Response(500))

    busy = httpx.MockTransport(lambda request: httpx.Response(429))
    await worker.enrich_hits({"transport": s2, "arxiv_transport": busy}, str(ws.id))
    assert all(r.abstract is None for r in stamped)
    assert result(await call("enrich_hits", workspace=ws.name))["queued"] == 2  # the stop left them queued

    fine = httpx.MockTransport(lambda request: httpx.Response(200, text=atom(id_list(request))))
    await worker.enrich_hits({"transport": s2, "arxiv_transport": fine}, str(ws.id))
    assert all(r.abstract for r in stamped)
    assert result(await call("enrich_hits", workspace=ws.name))["queued"] == 0


async def test_a_400_for_the_chunk_is_re_asked_one_id_at_a_time(session, waits):
    ok = arxiv_refs(2)
    odd = ref("odd", external_ids={"arxiv": "2301.99999"})  # well-formed, but arXiv rejects it
    junk = ref("junk", external_ids={"arxiv": "not an id"})  # never sent
    ws, _ = await pool(session, [*ok, odd, junk])
    sent = []

    def handler(request):
        ids = id_list(request)
        sent.append(ids)
        if "2301.99999" in ids and len(ids) > 1:
            return httpx.Response(400)
        return httpx.Response(200, text=atom(ids, skip=("2301.99999",)))

    async with arxiv_client(handler) as http:
        outcome = await hit_enrichment.arxiv_abstracts(session, http, [*ok, odd, junk])

    assert sent[0] == ["2301.00000", "2301.00001", "2301.99999"]
    assert sent[1:] == [["2301.00000"], ["2301.00001"], ["2301.99999"]]
    assert waits == [3.0, 3.0, 3.0]
    assert all(r.abstract for r in ok) and odd.abstract is None and junk.abstract is None
    assert (outcome.filled, outcome.rate_limited) == (2, False)


async def test_per_id_retry_stops_when_arxiv_throttles(session, waits):
    refs = arxiv_refs(3)
    ws, _ = await pool(session, refs)
    calls = []

    def handler(request):
        calls.append(id_list(request))
        return httpx.Response(400 if len(calls) == 1 else 429)

    async with arxiv_client(handler) as http:
        outcome = await hit_enrichment.arxiv_abstracts(session, http, refs)

    assert len(calls) == 1 + 4 and all(len(ids) == 1 for ids in calls[1:])  # no request for the other two ids
    assert outcome.rate_limited and outcome.filled == 0
    assert waits == [3.0, 2, 5, 15]


async def test_refs_for_arxiv_keeps_only_well_formed_arxiv_ids(session):
    good = ref("good", external_ids={"arxiv": "2301.00001v2"})
    junk = ref("junk", external_ids={"arxiv": "not an id"})
    ws, _ = await pool(session, [good, junk])
    assert [r.title for r in await hit_enrichment.refs_for_arxiv(session, ws.id, None)] == ["good"]
