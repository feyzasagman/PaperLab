import uuid
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.core import discovery, workspaces
from app.core.candidates import Candidate, normal_title
from app.core.paper_sources import SOURCES, SourceSettings
from app.core.prisma_export import PrismaExport, prisma_export
from app.core.workspace_search import (
    _find_or_create_external_ref,
    _insert_hit,
    search_batch,
    set_eligibility,
    snowball,
)
from app.models.references import ExternalRef
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchCursor, WorkspaceSearchHit, WorkspaceSearchRun
from app.providers import arxiv, discovery_fake, semantic_scholar

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

# Task 2's cursor convention (task-2-brief.md): 0 for arXiv/CORE/Crossref/S2 (offset-style), 1 for OpenAlex
# (1-based page number). Starting OpenAlex at 0 would ask its fake for a negative offset and get nothing back.
START_CURSOR = {"arxiv": 0, "semantic_scholar": 0, "openalex": 1, "unpaywall": 0}


@pytest.fixture
async def fake_providers():
    """Every source on, routed to discovery_fake's real MockTransport — same pattern as test_discovery_fake.py's own
    `fake_providers` fixture. search_batch's merge/dedup then runs against the real fake provider (Task 11's
    PAGE_PAPERS fixture), not a hand-rolled mock of provider responses."""
    every_source = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCES, True))
    providers = discovery.build_providers(every_source, discovery_fake.transport())
    yield providers
    await providers.aclose()


async def _new_run(session, sources: list[str]) -> WorkspaceSearchRun:
    workspace = Workspace(name=f"Engine test {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id,
        query_text="bert",
        filters_json={},
        query_overrides_json={},
        sources_json=sources,
        status="running",
        started_at=datetime.now(timezone.utc),
        stats_json={},
    )
    session.add(run)
    await session.flush()
    for source in sources:
        session.add(WorkspaceSearchCursor(run_id=run.id, source=source, cursor_json={"value": START_CURSOR[source]}))
    await session.commit()
    return run


async def test_search_batch_creates_hits_and_advances_cursors(session, fake_providers):
    run = await _new_run(session, ["arxiv"])

    result = await search_batch(session, fake_providers, run)

    assert result.new_hits >= 1
    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.run_id == run.id))).scalars().all()
    )
    assert len(hits) == result.new_hits
    assert all(h.acquisition_status == "not_attempted" for h in hits)

    cursor = (
        await session.execute(
            select(WorkspaceSearchCursor).where(
                WorkspaceSearchCursor.run_id == run.id, WorkspaceSearchCursor.source == "arxiv"
            )
        )
    ).scalar_one()
    assert cursor.exhausted  # PAGE_SIZE_BY_SOURCE["arxiv"] (20) exceeds the fixture's 3-item page


async def test_search_batch_dedupes_across_sources(session, fake_providers):
    # discovery_fake's PAGE_PAPERS fixture answers the same 3 papers (shared DOIs) from every source; the merge
    # step (reusing candidates.merge/_same_paper) must collapse them to one hit per paper, not one per source.
    run = await _new_run(session, ["arxiv", "semantic_scholar", "openalex"])

    result = await search_batch(session, fake_providers, run)

    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.run_id == run.id))).scalars().all()
    )
    titles = {h.normalized_title for h in hits}
    assert len(titles) == len(hits)  # no duplicate normalized titles from the same fixture paper
    assert len(hits) == 3  # 3 sources x 3 shared papers, merged down to 3 — not 9
    assert result.new_hits == 3


async def test_search_batch_preserves_existing_screening_state(session, fake_providers):
    # discovery_fake's PAGE_PAPERS[0] ("...Fixture 1", doi "...page-1") is what arxiv's page 0 returns first.
    # Pre-seed its ExternalRef + a WorkspaceSearchHit already screened by a user (this run's spec P2's global
    # constraint: a hit already in the pool — this run or an earlier one — keeps its screening state).
    run = await _new_run(session, ["arxiv"])
    title, doi = discovery_fake.PAGE_PAPERS[0]
    ref = ExternalRef(title=title, doi=doi)
    session.add(ref)
    await session.flush()
    existing_hit = WorkspaceSearchHit(
        workspace_id=run.workspace_id,
        run_id=run.id,
        external_ref_id=ref.id,
        source_method="database_search",
        normalized_title=normal_title(title),
        first_seen_at=datetime.now(timezone.utc),
        stage1_status="relevant",
        priority=2,
    )
    session.add(existing_hit)
    await session.commit()

    result = await search_batch(session, fake_providers, run)

    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.external_ref_id == ref.id)))
        .scalars()
        .all()
    )
    assert len(hits) == 1  # not duplicated by this batch
    assert hits[0].id == existing_hit.id
    assert hits[0].stage1_status == "relevant"  # untouched, not overwritten or reset
    assert hits[0].priority == 2
    assert result.new_hits == 2  # the other 2 PAGE_PAPERS are still new


async def test_snowball_backward_stores_the_seeds_references_as_hits(session, fake_providers):
    from app.models import Paper

    workspace = Workspace(name=f"snowball-test-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    seed = Paper(
        title="Attention Is All You Need", doi="10.5555/paperlab-e2e-free", file_path="/nonexistent.pdf"
    )  # doi matches a fixture arxiv id
    session.add(seed)
    await session.flush()
    await workspaces.add_paper(session, workspace.id, seed.id)

    result = await snowball(session, fake_providers, workspace.id, [seed.id], [], backward=True, forward=False)

    assert result.new_hits > 0
    assert result.skipped_seeds == []


async def test_snowball_keeps_one_directions_hits_when_the_other_direction_errors(session, fake_providers):
    """Fix-round finding: /references (backward) answers 200 while /citations (forward) 429s (spec §16: this is
    the default, unauthenticated-S2 case, not an edge case). Before the fix, `_snowball_from_s2` let the forward
    httpx.HTTPError propagate straight out of the whole call, and `snowball`'s try/except caught it at the
    per-SEED level — discarding the backward hits that had already come back successfully. Each direction must be
    wrapped separately: keep whatever came back, record the error for just the direction that failed."""
    from app.models import Paper

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/citations"):
            return httpx.Response(429)
        return discovery_fake._handle(request)

    mixed_s2_client = semantic_scholar.new_client(api_key=None, transport=httpx.MockTransport(handle))
    providers = replace(fake_providers, clients={**fake_providers.clients, "semantic_scholar": mixed_s2_client})

    workspace = Workspace(name=f"snowball-mixed-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    seed = Paper(title="Attention Is All You Need", doi="10.5555/paperlab-e2e-free", file_path="/nonexistent.pdf")
    session.add(seed)
    await session.flush()
    await workspaces.add_paper(session, workspace.id, seed.id)

    try:
        result = await snowball(session, providers, workspace.id, [seed.id], [], backward=True, forward=True)
    finally:
        await mixed_s2_client.aclose()

    assert result.new_hits > 0  # the backward hits were still stored
    assert result.skipped_seeds == []
    assert any("semantic_scholar" in key for key in result.errors)  # the forward failure is recorded
    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == workspace.id)))
        .scalars()
        .all()
    )
    assert all(h.source_method == "snowball_backward" for h in hits)  # no forward hits stored


async def test_snowball_skips_a_seed_semantic_scholar_does_not_know_but_still_processes_the_other(
    session, fake_providers
):
    """Review Focus: one seed alongside one Semantic Scholar has never heard of (its DOI isn't arXiv-style, so
    `s2_key` builds a "DOI:<doi>" key with no network call, but S2 itself answers 404 for that key — the same
    "unknown paper" signal a title-match miss would give). The unknown seed must land in `skipped_seeds` without
    raising, and the other, known seed must still produce hits — one seed's 404 must never abort the batch."""
    from app.models import Paper

    def handle(request: httpx.Request) -> httpx.Response:
        if "snowball-unknown" in str(request.url) and (
            request.url.path.endswith("/references") or request.url.path.endswith("/citations")
        ):
            return httpx.Response(404)
        return discovery_fake._handle(request)

    unknown_s2_client = semantic_scholar.new_client(api_key=None, transport=httpx.MockTransport(handle))
    providers = replace(fake_providers, clients={**fake_providers.clients, "semantic_scholar": unknown_s2_client})

    workspace = Workspace(name=f"snowball-unknown-seed-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    known_seed = Paper(
        title="Attention Is All You Need", doi="10.5555/paperlab-e2e-free", file_path="/nonexistent.pdf"
    )
    unknown_seed = Paper(
        title="A Paper Semantic Scholar Has Never Heard Of", doi="10.5555/paperlab-snowball-unknown",
        file_path="/nonexistent.pdf",
    )
    session.add_all([known_seed, unknown_seed])
    await session.flush()
    await workspaces.add_paper(session, workspace.id, known_seed.id)
    await workspaces.add_paper(session, workspace.id, unknown_seed.id)

    try:
        result = await snowball(
            session, providers, workspace.id, [unknown_seed.id, known_seed.id], [], backward=True, forward=False
        )
    finally:
        await unknown_s2_client.aclose()

    assert result.skipped_seeds == [unknown_seed.id]
    assert result.new_hits > 0
    assert result.errors == {}


async def test_snowball_does_not_duplicate_or_overwrite_an_existing_hit(session, fake_providers):
    # discovery_fake's S2 /references handler answers every key with the same three PAPERS fixtures (see its own
    # docstring: "any library paper cites the three papers"). Pre-seed PAPERS[0]'s (the free paper) ExternalRef +
    # a WorkspaceSearchHit already screened by a user, then snowball backward from a different seed that would
    # rediscover the same paper. Assert: still exactly one hit for that external_ref_id, stage1_status untouched —
    # mirrors test_search_batch_preserves_existing_screening_state, since _store_candidates_as_hits is the same
    # dedup path both features share.
    from app.models import Paper

    workspace = Workspace(name=f"snowball-dedup-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    seed = Paper(title="A Different Seed Paper", doi="10.5555/paperlab-e2e-landing", file_path="/nonexistent.pdf")
    session.add(seed)
    await session.flush()
    await workspaces.add_paper(session, workspace.id, seed.id)

    # discovery_fake's PAPERS DOIs are shared, fixed test fixtures also used by other flows (Find Papers Add,
    # references) against this same dev DB, so an ExternalRef for PAPERS[0] may already exist outside this test's
    # transaction. Resolving through _find_or_create_external_ref (the same path snowball itself uses) rather
    # than inserting a fresh row guarantees the pre-seeded hit attaches to the exact row snowball will later
    # dedup against, instead of racing an untiebroken DOI match against a leftover duplicate.
    free_title, free_doi, _ = discovery_fake.PAPERS[0]
    ref = await _find_or_create_external_ref(session, Candidate(title=free_title, doi=free_doi))
    await session.flush()
    prior_run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="prior database search", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(prior_run)
    await session.flush()
    existing_hit = WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=prior_run.id, external_ref_id=ref.id, source_method="database_search",
        normalized_title=normal_title(free_title), first_seen_at=datetime.now(timezone.utc),
        stage1_status="relevant", priority=2,
    )
    session.add(existing_hit)
    await session.commit()

    result = await snowball(session, fake_providers, workspace.id, [seed.id], [], backward=True, forward=False)

    hits = (
        (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.external_ref_id == ref.id)))
        .scalars()
        .all()
    )
    assert len(hits) == 1  # not duplicated by snowball rediscovering it
    assert hits[0].id == existing_hit.id
    assert hits[0].stage1_status == "relevant"  # untouched, not overwritten or reset
    assert hits[0].priority == 2
    assert hits[0].source_method == "database_search"  # not relabeled snowball_backward
    assert result.new_hits == 2  # the other 2 PAPERS fixtures (landing, closed) are still new


async def test_snowball_from_a_hit_seed_with_no_paper_ever_created(session, fake_providers):
    """The feature's whole point: a hit never imported (no free PDF, so import_hits left it with no Paper row at
    all) can still seed a snowball hop, because Semantic Scholar's citation lookup only ever needs doi/title
    (discovery.s2_key), both already on the hit's own ExternalRef — no Paper required."""
    workspace = Workspace(name=f"snowball-hit-seed-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    free_title, free_doi, _ = discovery_fake.PAPERS[0]
    ref = ExternalRef(title=free_title, doi=free_doi)
    session.add(ref)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="prior database search", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit = WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, external_ref_id=ref.id, source_method="database_search",
        normalized_title=normal_title(free_title), first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit)
    await session.commit()

    result = await snowball(session, fake_providers, workspace.id, [], [hit.id], backward=True, forward=False)

    assert result.new_hits > 0
    assert result.skipped_seeds == []
    assert result.errors == {}
    # The one piece of behavior unique to a hit-seeded hop (review finding): the stored rows must look like any
    # other snowball hit — tagged snowball_backward/round 1 — and must NOT carry seed_paper_id, since there's no
    # Paper row to attribute them to (the seed's own pre-existing hit, excluded below, is the only non-snowball
    # row in this workspace).
    new_rows = (
        (
            await session.execute(
                select(WorkspaceSearchHit).where(
                    WorkspaceSearchHit.workspace_id == workspace.id, WorkspaceSearchHit.id != hit.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(new_rows) == result.new_hits
    assert all(row.source_method == "snowball_backward" for row in new_rows)  # backward=True, forward=False
    assert all(row.snowball_round == 1 for row in new_rows)
    assert all(row.seed_paper_id is None for row in new_rows)


async def test_snowball_hit_seed_with_no_external_ref_id_is_skipped(session, fake_providers):
    """A hit with no linked ExternalRef has no doi/title to hop from — lands in skipped_seeds, not an error."""
    workspace = Workspace(name=f"snowball-hit-no-ref-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="prior database search", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit = WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, external_ref_id=None, source_method="database_search",
        normalized_title="no ref hit", first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit)
    await session.commit()

    result = await snowball(session, fake_providers, workspace.id, [], [hit.id], backward=True, forward=False)

    assert result.skipped_seeds == [hit.id]
    assert result.new_hits == 0
    assert result.errors == {}


async def test_snowball_hit_seed_in_different_workspace_raises_not_found(session, fake_providers):
    """Same ownership scoping as a paper seed (ownership check lives right next to it in `snowball`): a hit_id
    from another workspace must 404, not silently hop from a seed this workspace doesn't own."""
    from app.core.errors import NotFound

    workspace_a = Workspace(name=f"snowball-hit-ws-a-{uuid.uuid4().hex[:8]}")
    workspace_b = Workspace(name=f"snowball-hit-ws-b-{uuid.uuid4().hex[:8]}")
    session.add_all([workspace_a, workspace_b])
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace_a.id, query_text="prior database search", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit = WorkspaceSearchHit(
        workspace_id=workspace_a.id, run_id=run.id, external_ref_id=None, source_method="database_search",
        normalized_title="cross-workspace hit", first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit)
    await session.commit()

    with pytest.raises(NotFound):
        await snowball(session, fake_providers, workspace_b.id, [], [hit.id], backward=True, forward=False)


async def test_snowball_mixes_paper_and_hit_seeds_in_one_call(session, fake_providers):
    """One paper seed (already imported) and one hit seed (never imported) in the same call produce combined
    results — the two seed kinds aren't separate code paths from the caller's point of view.

    discovery_fake's own /references handler answers every key with the same PAPERS fixture, so a naive version
    of this test (both seeds, assert new_hits > 0) would pass even if the seed_hit_ids loop silently did nothing:
    the paper seed's backward hits alone are already > 0. A custom transport gives each seed's own key a
    DISTINCT, otherwise-unused candidate instead (review finding), so the only way to reach new_hits == 2 (an
    exact count, not just "> 0") is for both the seed_paper_ids loop AND the seed_hit_ids loop to each store
    their own one new hit."""
    from app.models import Paper

    free_title, free_doi, _ = discovery_fake.PAPERS[0]
    landing_title, landing_doi, _ = discovery_fake.PAPERS[1]
    paper_seed_ref_doi = "10.5555/paperlab-mixed-from-paper-seed"
    hit_seed_ref_doi = "10.5555/paperlab-mixed-from-hit-seed"

    def _s2_paper(paper_id: str, title: str, doi: str) -> dict:
        return {
            "paperId": paper_id, "title": title, "year": 2026, "venue": "Journal of Fixtures",
            "authors": [{"name": "Ada Fixture"}], "externalIds": {"DOI": doi},
            "openAccessPdf": {"url": ""}, "citationCount": 3,
        }

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path.endswith("/references"):
            if free_doi in url:  # the paper seed's own key
                cited = _s2_paper("a" * 40, "Paper Seed's Own Backward Reference", paper_seed_ref_doi)
                return httpx.Response(200, json={"offset": 0, "data": [{"citedPaper": cited}]})
            if landing_doi in url:  # the hit seed's own key
                cited = _s2_paper("b" * 40, "Hit Seed's Own Backward Reference", hit_seed_ref_doi)
                return httpx.Response(200, json={"offset": 0, "data": [{"citedPaper": cited}]})
        return discovery_fake._handle(request)

    mixed_s2_client = semantic_scholar.new_client(api_key=None, transport=httpx.MockTransport(handle))
    providers = replace(fake_providers, clients={**fake_providers.clients, "semantic_scholar": mixed_s2_client})

    workspace = Workspace(name=f"snowball-mixed-seeds-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    paper_seed = Paper(title=free_title, doi=free_doi, file_path="/nonexistent.pdf")
    session.add(paper_seed)
    await session.flush()
    await workspaces.add_paper(session, workspace.id, paper_seed.id)

    ref = ExternalRef(title=landing_title, doi=landing_doi)
    session.add(ref)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="prior database search", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit_seed = WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, external_ref_id=ref.id, source_method="database_search",
        normalized_title=normal_title(landing_title), first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit_seed)
    await session.commit()

    try:
        result = await snowball(
            session, providers, workspace.id, [paper_seed.id], [hit_seed.id], backward=True, forward=False
        )
    finally:
        await mixed_s2_client.aclose()

    assert result.new_hits == 2  # exactly one new hit per seed's own distinct reference — not just "> 0"
    assert result.skipped_seeds == []
    assert result.errors == {}

    new_refs = (
        await session.execute(
            select(ExternalRef.doi).join(
                WorkspaceSearchHit, WorkspaceSearchHit.external_ref_id == ExternalRef.id
            ).where(WorkspaceSearchHit.workspace_id == workspace.id, WorkspaceSearchHit.id != hit_seed.id)
        )
    ).scalars().all()
    # Both the paper seed's and the hit seed's own distinct reference made it into the pool — proof the
    # seed_hit_ids loop actually ran and stored its own candidate, not just the seed_paper_ids loop's.
    assert set(new_refs) == {paper_seed_ref_doi, hit_seed_ref_doi}


async def test_search_batch_records_source_error_without_failing_run(session, fake_providers):
    broken_arxiv = arxiv.new_client(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    providers = replace(fake_providers, clients={**fake_providers.clients, "arxiv": broken_arxiv})
    run = await _new_run(session, ["arxiv"])

    try:
        result = await search_batch(session, providers, run)
    finally:
        await broken_arxiv.aclose()

    assert "arxiv" in result.errors
    assert result.new_hits == 0
    cursor = (
        await session.execute(
            select(WorkspaceSearchCursor).where(
                WorkspaceSearchCursor.run_id == run.id, WorkspaceSearchCursor.source == "arxiv"
            )
        )
    ).scalar_one()
    assert cursor.last_error is not None
    assert not cursor.exhausted


async def test_search_batch_skips_a_cooling_down_source_without_counting_a_new_error(session, fake_providers, monkeypatch):
    """A source that just errored is skipped entirely on the very next call, not retried immediately — real
    consecutive-iteration retries land within a couple of pacing intervals of each other (a few seconds), which
    doesn't give a brief/transient error (arXiv's API has answered a plain 406 to an otherwise-valid, identical
    request that then succeeded seconds later) any real chance to clear before the source is abandoned."""
    from app.core import workspace_search as core_module

    fails_once = [True]

    async def flaky_arxiv_search(http, query, page_size, cursor):
        if fails_once[0]:
            fails_once[0] = False
            raise httpx.HTTPError("transient 406")
        return [], None

    monkeypatch.setitem(core_module._PAGE_FUNCS, "arxiv", flaky_arxiv_search)
    fake_clock = iter([datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)])
    monkeypatch.setattr(core_module, "_now", lambda: next(fake_clock))

    run = await _new_run(session, ["arxiv"])
    await search_batch(session, fake_providers, run)  # first call: errors, starts a 10s cooldown
    cursor = (
        await session.execute(
            select(WorkspaceSearchCursor).where(
                WorkspaceSearchCursor.run_id == run.id, WorkspaceSearchCursor.source == "arxiv"
            )
        )
    ).scalar_one()
    assert cursor.cursor_json["errors"] == 1

    # Second call, one fake second later — still well inside the cooldown. Must be skipped entirely: no new
    # attempt (flaky_arxiv_search would raise AssertionError-free either way here, so the real signal is the
    # error count staying put, not incrementing to 2).
    result = await search_batch(session, fake_providers, run)
    await session.refresh(cursor)
    assert "arxiv" not in result.errors
    assert cursor.cursor_json["errors"] == 1
    assert not cursor.exhausted


async def test_search_batch_retries_a_cooled_down_source_and_a_success_clears_the_error_count(session, fake_providers, monkeypatch):
    """Once the cooldown has genuinely elapsed, the source is tried again — and a page that succeeds this time
    clears the error streak entirely (same as a fresh cursor), not just decrements it."""
    from app.core import workspace_search as core_module

    fails_once = [True]

    async def flaky_arxiv_search(http, query, page_size, cursor):
        if fails_once[0]:
            fails_once[0] = False
            raise httpx.HTTPError("transient 406")
        return [], None  # succeeds and exhausts cleanly on the retry

    monkeypatch.setitem(core_module._PAGE_FUNCS, "arxiv", flaky_arxiv_search)
    far_future = iter([datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 1, tzinfo=timezone.utc)])
    monkeypatch.setattr(core_module, "_now", lambda: next(far_future))

    run = await _new_run(session, ["arxiv"])
    await search_batch(session, fake_providers, run)  # errors, starts a cooldown

    result = await search_batch(session, fake_providers, run)  # an hour later: well past even the longest backoff

    assert "arxiv" not in result.errors
    assert result.sources_exhausted == ["arxiv"]  # the retry ran (not skipped) and succeeded straight to exhaustion
    cursor = (
        await session.execute(
            select(WorkspaceSearchCursor).where(
                WorkspaceSearchCursor.run_id == run.id, WorkspaceSearchCursor.source == "arxiv"
            )
        )
    ).scalar_one()
    assert cursor.exhausted  # this fake's next_cursor is None — a real, clean exhaustion, not an error-cap trip
    assert cursor.last_error is None  # cleared unconditionally on any successful page, exhausting or not


async def test_search_batch_marks_a_disabled_source_cursor_exhausted(session):
    """A source whose Providers.client() is None (Unpaywall with no contact_email configured — the
    fresh-database default) has nothing to fetch. Its cursor must still flip to exhausted immediately, or the
    worker's `all(c.exhausted for c in cursors)` check can never become true (M30a Task 17 E2E bug: a source
    stuck at exhausted=False forever turns the worker's paging loop into a busy-loop)."""
    providers = discovery.Providers(pdf=httpx.AsyncClient())  # every source left off (clients defaults to {})
    try:
        run = await _new_run(session, ["unpaywall"])
        result = await search_batch(session, providers, run)
    finally:
        await providers.pdf.aclose()

    assert result.new_hits == 0
    cursor = (
        await session.execute(
            select(WorkspaceSearchCursor).where(
                WorkspaceSearchCursor.run_id == run.id, WorkspaceSearchCursor.source == "unpaywall"
            )
        )
    ).scalar_one()
    assert cursor.exhausted
    assert cursor.last_error is None  # not an error state — there was nothing to do here


@pytest.mark.asyncio
async def test_start_run_creates_cursors_for_each_source(session):
    from app.core.workspace_search import start_run
    workspace = Workspace(name=f"Lifecycle {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()

    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv", "openalex"])

    assert run.status == "running"
    cursors = (await session.execute(
        select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id)
    )).scalars().all()
    assert {c.source for c in cursors} == {"arxiv", "openalex"}


@pytest.mark.asyncio
async def test_stop_run_sets_status_and_stopped_at(session):
    from app.core.workspace_search import start_run, stop_run
    workspace = Workspace(name=f"Stop {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()
    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv"])

    stopped = await stop_run(session, run.id)

    assert stopped.status == "stopped"
    assert stopped.stopped_at is not None


@pytest.mark.asyncio
async def test_start_run_on_a_stopped_run_resumes_its_cursors(session):
    from app.core.workspace_search import start_run, stop_run
    workspace = Workspace(name=f"Resume {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()
    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv"])
    cursor = (await session.execute(
        select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id)
    )).scalar_one()
    cursor.cursor_json = {"value": 40}
    await session.commit()
    await stop_run(session, run.id)

    resumed = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv"], run_id=run.id)

    assert resumed.id == run.id
    assert resumed.status == "running"
    reloaded_cursor = (await session.execute(
        select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id)
    )).scalar_one()
    assert reloaded_cursor.cursor_json == {"value": 40}


@pytest.mark.asyncio
async def test_start_run_raises_conflict_on_already_running(session):
    from app.core.errors import Conflict
    from app.core.workspace_search import start_run
    workspace = Workspace(name=f"Conflict {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()
    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv"])

    with pytest.raises(Conflict):
        await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv"], run_id=run.id)


@pytest.mark.asyncio
async def test_start_run_raises_not_found_for_cross_workspace_run(session):
    from app.core.errors import NotFound
    from app.core.workspace_search import start_run, stop_run
    workspace_a = Workspace(name=f"Workspace A {uuid.uuid4().hex[:8]}")
    workspace_b = Workspace(name=f"Workspace B {uuid.uuid4().hex[:8]}")
    session.add(workspace_a)
    session.add(workspace_b)
    await session.flush()
    await session.commit()
    run = await start_run(session, workspace_a.id, "bert", filters={}, sources=["arxiv"])
    await stop_run(session, run.id)

    with pytest.raises(NotFound):
        await start_run(session, workspace_b.id, "bert", filters={}, sources=["arxiv"], run_id=run.id)


@pytest.mark.asyncio
async def test_start_run_seeds_openalex_cursor_at_one_not_zero(session):
    """OpenAlex's `page` param is 1-based; every other source's cursor is a 0-based offset (I4). Seeding OpenAlex
    at 0 asked its fake for a negative offset and got nothing back "by coincidence," hiding the bug."""
    from app.core.workspace_search import start_run
    workspace = Workspace(name=f"OpenAlex seed {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()

    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv", "openalex"])

    cursors = {
        c.source: c.cursor_json
        for c in (
            await session.execute(select(WorkspaceSearchCursor).where(WorkspaceSearchCursor.run_id == run.id))
        ).scalars()
    }
    assert cursors["openalex"] == {"value": 1}
    assert cursors["arxiv"] == {"value": 0}


@pytest.mark.asyncio
async def test_start_run_orders_sources_by_paper_sources_trust_order(session):
    """D73's merge trust order is app/core/paper_sources.SOURCES; search_batch's `found` dict is insertion-ordered
    and feeds merge()'s ranking directly, so storing sources in whatever order the caller sent them would let the
    caller's field order silently become the trust order instead (bundled minor)."""
    from app.core.workspace_search import start_run
    workspace = Workspace(name=f"Source order {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()

    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv", "openalex", "crossref"])

    assert run.sources_json == ["openalex", "crossref", "arxiv"]  # paper_sources.SOURCES order, not caller order


@pytest.mark.asyncio
async def test_start_run_rejects_non_empty_filters(session):
    """Real per-source filter translation is out of scope for this fix wave; accepting and silently ignoring a
    filter would misreport the search strategy a PRISMA methods section later cites (I6 part 2)."""
    from app.core.errors import InvalidInput
    from app.core.workspace_search import start_run
    workspace = Workspace(name=f"Filters rejected {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()

    with pytest.raises(InvalidInput):
        await start_run(session, workspace.id, "bert", filters={"open_access_only": True}, sources=["arxiv"])


@pytest.mark.asyncio
async def test_stop_run_on_a_non_running_run_is_conflict(session):
    """Stopping only applies to a run actually in flight — same "doesn't apply to the current state" convention
    as start_run's own Conflict on an already-running run (bundled minor)."""
    from app.core.errors import Conflict
    from app.core.workspace_search import start_run, stop_run
    workspace = Workspace(name=f"Stop conflict {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    await session.commit()
    run = await start_run(session, workspace.id, "bert", filters={}, sources=["arxiv"])
    await stop_run(session, run.id)

    with pytest.raises(Conflict):
        await stop_run(session, run.id)


async def test_search_batch_uses_the_per_source_query_override(session, fake_providers, monkeypatch):
    """P11's per-source override (run.query_overrides_json) must actually reach that source's search_page call —
    before this fix it was accepted, stored, and silently ignored (I6 part 1)."""
    import app.core.workspace_search as core_module

    seen_queries = []
    real_page = arxiv.search_page

    async def recording_page(http, query, page_size, cursor):
        seen_queries.append(query)
        return await real_page(http, query, page_size, cursor)

    monkeypatch.setitem(core_module._PAGE_FUNCS, "arxiv", recording_page)

    run = await _new_run(session, ["arxiv"])
    run.query_overrides_json = {"arxiv": "transformer architectures"}
    await session.commit()

    await search_batch(session, fake_providers, run)

    assert seen_queries == ["transformer architectures"]  # not run.query_text ("bert")


async def test_find_or_create_external_ref_prefers_the_doi_match_over_a_weaker_identifier_match(session):
    """Two existing rows: ref_a shares the candidate's DOI; ref_b only shares the candidate's s2_id and has its
    own, unrelated DOI. The OR-match hits both. Before Important 2's ordering fix, `.first()` on the unordered
    result could pick either row arbitrarily — picking ref_b would attach this hit (and later, its import/PDF
    download) to the wrong paper entirely, not just risk a bad backfill. DOI is the strongest identifier, so
    ref_a must win deterministically, and the fix must still not crash or cross-write onto ref_b (this exact
    backfill mechanism corrupted a real row once — the ledger's "Closed Access Fixture" incident)."""
    ref_a = ExternalRef(title="Ref A", doi="10.5555/paperlab-i3-a")
    ref_b = ExternalRef(
        title="Ref B", doi="10.5555/paperlab-i3-b", external_ids={"semantic_scholar": "i3-shared-s2-id"}
    )
    session.add_all([ref_a, ref_b])
    await session.commit()

    candidate = Candidate(
        title="New candidate",
        doi="10.5555/paperlab-i3-a",
        external_ids={"semantic_scholar": "i3-shared-s2-id"},
        pdf_urls=["https://example.test/candidate.pdf"],
    )

    ref = await _find_or_create_external_ref(session, candidate)
    await session.commit()  # must not raise IntegrityError

    assert ref.id == ref_a.id  # the DOI match wins deterministically, not ref_b

    # "i3-shared-s2-id" must still belong to exactly one row — never duplicated onto ref_a.
    holders = (
        await session.execute(
            select(ExternalRef.id).where(ExternalRef.external_ids["semantic_scholar"].astext == "i3-shared-s2-id")
        )
    ).scalars().all()
    assert holders == [ref_b.id]

    await session.refresh(ref_a)
    await session.refresh(ref_b)
    assert ref_a.doi == "10.5555/paperlab-i3-a"  # never overwritten by the candidate's own doi
    assert ref_b.doi == "10.5555/paperlab-i3-b"  # untouched — never even loaded as the matched row
    assert ref_a.pdf_urls == candidate.pdf_urls  # ref_a's own DOI matches the candidate's, so this is safe


async def test_find_or_create_external_ref_sets_sources_on_creation(session):
    candidate = Candidate(title="New paper", doi="10.5555/paperlab-sources-a", sources=("arxiv", "openalex"))

    ref = await _find_or_create_external_ref(session, candidate)
    await session.commit()

    assert ref.sources == ["arxiv", "openalex"]


async def test_find_or_create_external_ref_unions_sources_on_a_later_rediscovery(session):
    """A paper first found via arxiv, then later rediscovered (a new search run, or snowball) via a source that
    hadn't found it before — both should end up recorded, not just whichever created the row, and never
    duplicated if the same source finds it again."""
    existing = ExternalRef(title="Existing paper", doi="10.5555/paperlab-sources-b", sources=["arxiv"])
    session.add(existing)
    await session.commit()

    candidate = Candidate(title="Existing paper", doi="10.5555/paperlab-sources-b", sources=("openalex", "arxiv"))
    ref = await _find_or_create_external_ref(session, candidate)
    await session.commit()

    assert ref.id == existing.id
    assert ref.sources == ["arxiv", "openalex"]  # existing order kept, "openalex" appended once, "arxiv" not duplicated


async def test_insert_hit_is_race_safe_under_a_duplicate_attempt(session):
    """Proxy for two search_batch calls racing on the same (workspace_id, external_ref_id) after a
    stop-then-immediate-restart (enqueue_job has no dedup job id): this suite's per-test session is one
    savepoint-wrapped connection, so two genuinely overlapping transactions aren't reproducible here — two
    sequential calls sharing the same candidate/ref is the accepted proxy (I2 part 2)."""
    workspace = Workspace(name=f"Insert race {uuid.uuid4().hex[:8]}")
    session.add(workspace)
    ref = ExternalRef(title="Racing Paper", doi="10.5555/paperlab-i2-race")
    session.add(ref)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="running", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    await session.commit()
    candidate = Candidate(title="Racing Paper", doi="10.5555/paperlab-i2-race")

    first = await _insert_hit(session, run, ref, candidate)
    second = await _insert_hit(session, run, ref, candidate)  # the "concurrent" duplicate attempt

    assert (first, second) == (True, False)  # no crash on the duplicate; only the first actually inserted
    count = await session.scalar(
        select(func.count()).select_from(WorkspaceSearchHit).where(WorkspaceSearchHit.external_ref_id == ref.id)
    )
    assert count == 1


async def test_search_run_eligibility_round_trips(session):
    from app.models import Paper
    from app.models.workspace_search import SearchRunEligibility

    workspace = Workspace(name=f"eligibility-test-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    paper = Paper(title="Seed Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()

    row = SearchRunEligibility(
        paper_id=paper.id, search_run_id=run.id, stage2_status="include",
        assessed_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()

    fetched = await session.get(SearchRunEligibility, (paper.id, run.id))
    assert fetched.stage2_status == "include"


async def test_set_eligibility_upserts_on_a_second_call(session):
    """First call inserts a (paper_id, run_id) row; a second call for the same pair updates it in place instead
    of inserting a duplicate — same upsert discipline the M30a screening PATCH already has for stage1 fields."""
    from app.models import Paper
    from app.models.workspace_search import SearchRunEligibility

    workspace = Workspace(name=f"eligibility-upsert-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    paper = Paper(title="Upsert Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    await workspaces.add_paper(session, workspace.id, paper.id)
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    await session.commit()

    await set_eligibility(session, workspace.id, paper.id, run.id, "include", None)

    row = await session.get(SearchRunEligibility, (paper.id, run.id))
    assert row.stage2_status == "include"
    assert row.stage2_exclude_reason is None

    await set_eligibility(session, workspace.id, paper.id, run.id, "exclude", "not_relevant")

    count = await session.scalar(
        select(func.count()).select_from(SearchRunEligibility).where(
            SearchRunEligibility.paper_id == paper.id, SearchRunEligibility.search_run_id == run.id
        )
    )
    assert count == 1  # still exactly one row for (paper_id, run_id) — updated, not duplicated
    row = await session.get(SearchRunEligibility, (paper.id, run.id))
    assert row.stage2_status == "exclude"
    assert row.stage2_exclude_reason == "not_relevant"


async def test_set_eligibility_raises_not_found_for_a_paper_outside_the_workspace(session):
    from app.models import Paper

    workspace = Workspace(name=f"eligibility-owner-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    paper = Paper(title="Outsider Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", sources_json=["arxiv"], status="exhausted",
        started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    await session.commit()
    # paper deliberately never added to the workspace via workspaces.add_paper

    from app.core.errors import NotFound

    with pytest.raises(NotFound):
        await set_eligibility(session, workspace.id, paper.id, run.id, "include", None)


async def test_prisma_combined_counts_the_full_funnel(session):
    """One run, 5 hits. Hand count (source_method matters this time — see below):
      hit1: stage1 not_relevant/wrong_topic, database_search
      hit2: stage1 not_relevant/duplicate,   database_search
      hit3: stage1 relevant, imported, paper A, database_search
      hit4: stage1 relevant, imported, paper B, database_search
      hit5: stage1 relevant, failed,   no paper, snowball_backward  <- the one snowball hit
    Paper A eligibility: include. Paper B eligibility: exclude/out_of_scope.
    run.stats_json.per_source_raw_count = {"arxiv": 6, "openalex": 2} -> stats sum = 8.

    identified = stats sum (8) + count of hits whose source_method != "database_search" (hit5 only) = 9.
    duplicates_removed = max(identified(9) - len(hits)(5), 0) = 4.
    stage1_screened = 5 (all 5 have a stage1_status).
    stage1_excluded = 2 (hit1, hit2); by_reason = {"wrong_topic": 1, "duplicate": 1}.
    sought = 3 (hit3, hit4, hit5 - all stage1 relevant).
    not_retrieved = 1 (hit5's acquisition_status "failed" is not in ("imported", "manual")).
    stage2_assessed = 2 (paper A, paper B - the only relevant hits carrying a paper_id).
    stage2_excluded = 1 (paper B); by_reason = {"out_of_scope": 1}.
    included = 1 (paper A).
    """
    from app.models import Paper
    from app.models.workspace_search import SearchRunEligibility

    run = await _new_run(session, ["arxiv"])
    run.stats_json = {"per_source_raw_count": {"arxiv": 6, "openalex": 2}}

    paper_a = Paper(title="Paper A", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    paper_b = Paper(title="Paper B", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add_all([paper_a, paper_b])
    await session.flush()

    now = datetime.now(timezone.utc)

    def hit(**overrides) -> WorkspaceSearchHit:
        base = dict(
            workspace_id=run.workspace_id, run_id=run.id, source_method="database_search",
            normalized_title=f"hit-{uuid.uuid4().hex[:8]}", first_seen_at=now,
        )
        base.update(overrides)
        return WorkspaceSearchHit(**base)

    session.add_all([
        hit(stage1_status="not_relevant", stage1_exclude_reason="wrong_topic"),
        hit(stage1_status="not_relevant", stage1_exclude_reason="duplicate"),
        hit(stage1_status="relevant", acquisition_status="imported", paper_id=paper_a.id),
        hit(stage1_status="relevant", acquisition_status="imported", paper_id=paper_b.id),
        hit(
            stage1_status="relevant", acquisition_status="failed", source_method="snowball_backward",
        ),
    ])
    session.add_all([
        SearchRunEligibility(
            paper_id=paper_a.id, search_run_id=run.id, stage2_status="include", assessed_at=now,
        ),
        SearchRunEligibility(
            paper_id=paper_b.id, search_run_id=run.id, stage2_status="exclude",
            stage2_exclude_reason="out_of_scope", assessed_at=now,
        ),
    ])
    await session.commit()

    result = await prisma_export(session, run.workspace_id, run_id=None)

    assert isinstance(result, PrismaExport)
    assert result.identified == 9
    assert result.duplicates_removed == 4
    assert result.stage1_screened == 5
    assert result.stage1_excluded == 2
    assert result.stage1_excluded_by_reason == {"wrong_topic": 1, "duplicate": 1}
    assert result.sought == 3
    assert result.not_retrieved == 1
    assert result.stage2_assessed == 2
    assert result.stage2_excluded == 1
    assert result.stage2_excluded_by_reason == {"out_of_scope": 1}
    assert result.included == 1
    assert result.runs == [
        {
            "id": str(run.id), "query_text": run.query_text, "filters_json": run.filters_json,
            "started_at": run.started_at.isoformat(),
        }
    ]


async def test_prisma_combined_uses_the_most_recently_assessed_verdict_across_two_runs(session):
    """Same paper, two different runs, two different eligibility verdicts with two DIFFERENT assessed_at
    timestamps. The later-timed row (run_b's, "include") must win over the earlier-timed row (run_a's,
    "exclude") in the combined view - per spec §13's explicit edge case. run_b's row is added to the session
    FIRST, run_a's SECOND, so a pass just because insertion order happened to match assessed_at order can't
    happen: the code has to actually compare timestamps."""
    from app.models import Paper
    from app.models.workspace_search import SearchRunEligibility

    workspace = Workspace(name=f"prisma-tiebreak-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    paper = Paper(title="Reassessed Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()

    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 6, 1, tzinfo=timezone.utc)

    run_a = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q-a", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=earlier, stats_json={},
    )
    run_b = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q-b", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=later, stats_json={},
    )
    session.add_all([run_a, run_b])
    await session.flush()

    # Only one hit is needed to put the paper "in corpus" for the combined view (relevant + a paper_id) - it
    # doesn't matter which run found it.
    session.add(
        WorkspaceSearchHit(
            workspace_id=workspace.id, run_id=run_a.id, source_method="database_search",
            normalized_title="reassessed-paper", first_seen_at=earlier, stage1_status="relevant",
            acquisition_status="imported", paper_id=paper.id,
        )
    )
    # run_b's (later assessed_at) row added first, run_a's (earlier assessed_at) second - insertion order is the
    # OPPOSITE of assessed_at order, so a code path that just kept "whichever row it saw last" would win here too
    # and this test wouldn't catch it; only comparing assessed_at explicitly picks run_b's "include".
    session.add(
        SearchRunEligibility(
            paper_id=paper.id, search_run_id=run_b.id, stage2_status="include", assessed_at=later,
        )
    )
    session.add(
        SearchRunEligibility(
            paper_id=paper.id, search_run_id=run_a.id, stage2_status="exclude",
            stage2_exclude_reason="wrong_topic", assessed_at=earlier,
        )
    )
    await session.commit()

    result = await prisma_export(session, workspace.id, run_id=None)

    assert result.stage2_assessed == 1  # one paper, one counted verdict
    assert result.included == 1  # run_b's later "include" wins
    assert result.stage2_excluded == 0  # run_a's earlier "exclude" is shadowed, not counted


async def test_prisma_per_run_identified_count_comes_from_stats_json_not_hit_count(session):
    """run_2's stats_json.per_source_raw_count sums to 10 (what it actually found, pre-dedup), but only 2
    WorkspaceSearchHit rows carry run_2's own run_id: hit_new (a genuinely new database_search find) and
    hit_snowball (a snowball_backward hit, attributed to run_2 for this fixture only to also prove the
    snowball-counts-toward-identified term applies in the per-run branch, not just combined). The other 8 things
    run_2's search turned up were already in the pool from an earlier run (run_1) and kept run_1's run_id per
    _store_candidates_as_hits' dedup rule, so they don't carry run_2's run_id at all.

    identified must be stats_json sum (10) + 1 snowball hit = 11 - NOT len(hits with run_id=run_2) (2), and NOT
    the bare stats_json sum alone (10). All three numbers differ, so using the wrong formula is caught here.
    duplicates_removed = max(11 - 2, 0) = 9.
    """
    workspace = Workspace(name=f"prisma-per-run-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    now = datetime.now(timezone.utc)

    run_1 = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q1", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=now, stats_json={"per_source_raw_count": {"arxiv": 8}},
    )
    run_2 = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q2", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=now,
        stats_json={"per_source_raw_count": {"arxiv": 10}},
    )
    session.add_all([run_1, run_2])
    await session.flush()

    # The 8 items run_2's search re-found that were already in the pool: they stayed attributed to run_1, not
    # run_2 (_store_candidates_as_hits keeps a re-found hit's original run/screening state).
    already_in_pool = [
        WorkspaceSearchHit(
            workspace_id=workspace.id, run_id=run_1.id, source_method="database_search",
            normalized_title=f"already-{i}", first_seen_at=now,
        )
        for i in range(8)
    ]
    hit_new = WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run_2.id, source_method="database_search",
        normalized_title="run2-new-find", first_seen_at=now,
    )
    hit_snowball = WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run_2.id, source_method="snowball_backward",
        normalized_title="run2-snowball-find", first_seen_at=now,
    )
    session.add_all([*already_in_pool, hit_new, hit_snowball])
    await session.commit()

    result = await prisma_export(session, workspace.id, run_id=run_2.id)

    assert result.identified == 11  # stats_json sum (10) + 1 snowball hit - not 2 (hit count), not 10 (stats alone)
    assert result.duplicates_removed == 9
    assert result.runs == []  # per-run export: caller already knows which run


async def test_prisma_combined_does_not_leak_another_workspaces_eligibility_verdict(session):
    """Papers are shared/reusable entities, not 1:1 with a workspace (workspace_papers is the join table -
    confirmed by test_set_eligibility_raises_not_found_for_a_paper_outside_the_workspace and by
    upload_hit_pdf's separate workspaces.add_paper association step). The SAME physical paper independently
    screened in two DIFFERENT workspaces, via two DIFFERENT runs, must never let one workspace's combined
    export pick up the other workspace's SearchRunEligibility verdict - even when that other verdict is the
    more recently assessed_at one, which is exactly the condition that would make a missing workspace scope on
    elig_query silently "win" via the most-recent-verdict tie-break."""
    from app.models import Paper
    from app.models.workspace_search import SearchRunEligibility

    workspace_a = Workspace(name=f"prisma-leak-a-{uuid.uuid4().hex[:8]}")
    workspace_b = Workspace(name=f"prisma-leak-b-{uuid.uuid4().hex[:8]}")
    session.add_all([workspace_a, workspace_b])
    paper = Paper(title="Shared Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    await workspaces.add_paper(session, workspace_a.id, paper.id)
    await workspaces.add_paper(session, workspace_b.id, paper.id)

    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 6, 1, tzinfo=timezone.utc)  # workspace B's verdict is the more recent one

    run_a = WorkspaceSearchRun(
        workspace_id=workspace_a.id, query_text="q-a", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=earlier, stats_json={},
    )
    run_b = WorkspaceSearchRun(
        workspace_id=workspace_b.id, query_text="q-b", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=later, stats_json={},
    )
    session.add_all([run_a, run_b])
    await session.flush()

    session.add_all([
        WorkspaceSearchHit(
            workspace_id=workspace_a.id, run_id=run_a.id, source_method="database_search",
            normalized_title="shared-paper-in-a", first_seen_at=earlier, stage1_status="relevant",
            acquisition_status="imported", paper_id=paper.id,
        ),
        WorkspaceSearchHit(
            workspace_id=workspace_b.id, run_id=run_b.id, source_method="database_search",
            normalized_title="shared-paper-in-b", first_seen_at=later, stage1_status="relevant",
            acquisition_status="imported", paper_id=paper.id,
        ),
    ])
    session.add_all([
        SearchRunEligibility(
            paper_id=paper.id, search_run_id=run_a.id, stage2_status="include", assessed_at=earlier,
        ),
        SearchRunEligibility(
            paper_id=paper.id, search_run_id=run_b.id, stage2_status="exclude",
            stage2_exclude_reason="wrong_topic", assessed_at=later,
        ),
    ])
    await session.commit()

    result_a = await prisma_export(session, workspace_a.id, run_id=None)
    result_b = await prisma_export(session, workspace_b.id, run_id=None)

    # Workspace A must see only its OWN verdict (include from run_a) - NOT workspace B's later-assessed
    # "exclude", even though "exclude" is the more recently assessed_at row workspace-blind code would pick.
    assert result_a.stage2_assessed == 1
    assert result_a.included == 1
    assert result_a.stage2_excluded == 0

    # And vice versa: workspace B must see only its own "exclude", not workspace A's earlier "include".
    assert result_b.stage2_assessed == 1
    assert result_b.stage2_excluded == 1
    assert result_b.stage2_excluded_by_reason == {"wrong_topic": 1}
    assert result_b.included == 0


async def test_search_batch_drops_known_out_of_range_years_and_keeps_undated(session, fake_providers):
    run = await _new_run(session, ["arxiv"])
    run.filters_json = {"year_from": 2100}  # every dated fixture paper is older
    await session.commit()

    result = await search_batch(session, fake_providers, run)

    assert result.out_of_range["arxiv"] >= 1
    assert result.raw_counts["arxiv"] + result.out_of_range["arxiv"] == 3  # PAGE_PAPERS page size
    assert result.new_hits == result.raw_counts["arxiv"]  # only undated fixtures, if any, are stored
