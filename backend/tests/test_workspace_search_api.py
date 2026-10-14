import uuid
from datetime import datetime, timezone

import pytest

from app.api.deps import get_discovery
from app.core import discovery
from app.core.paper_sources import SOURCES, SourceSettings
from app.providers import discovery_fake

pytestmark = pytest.mark.anyio


@pytest.fixture
def discovery_api(app, discovery_fakes):
    """Same pattern as test_discovery_api.py's fixture: routes DiscoveryDep to the fake providers so import_hits'
    download_pdf call has a real (fake) PDF host to hit."""
    app.dependency_overrides[get_discovery] = lambda: discovery_fakes.providers
    return discovery_fakes


@pytest.fixture
async def fake_providers_all(app):
    """Every source on, routed to discovery_fake's MockTransport — for snowball and search_batch tests."""
    every_source = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCES, True))
    providers = discovery.build_providers(every_source, discovery_fake.transport())
    app.dependency_overrides[get_discovery] = lambda: providers
    yield providers
    await providers.aclose()


async def test_start_run_over_http(client):
    ws = await client.post("/api/workspaces", json={"name": "Search API test"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["arxiv"]},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "running"
    assert body["query_text"] == "bert"


async def test_start_run_response_exposes_query_overrides(client):
    """A run's recorded search strategy (what a PRISMA methods section later cites) must show what was actually
    submitted, per-source overrides included — SearchRunOut had no such field before this fix (I6 part 3)."""
    ws = await client.post("/api/workspaces", json={"name": "Query overrides test"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs",
        json={
            "query": "bert", "filters": {}, "sources": ["arxiv", "openalex"],
            "query_overrides": {"arxiv": "bert language model"},
        },
    )

    assert resp.status_code == 201
    assert resp.json()["query_overrides_json"] == {"arxiv": "bert language model"}


async def test_start_run_rejects_an_unsupported_source(client):
    """Unpaywall is DOI-only PDF enrichment, never a discovery source (spec §6/app/core/paper_sources.py's own
    comment: "Unpaywall only adds PDF links") — search_batch's _PAGE_FUNCS has no "unpaywall" entry, so sending it
    used to reach a KeyError inside the worker and crash the whole run instead of a clean 422 (C1)."""
    ws = await client.post("/api/workspaces", json={"name": "Unsupported source test"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["unpaywall"]},
    )

    assert resp.status_code == 422


async def test_start_run_rejects_an_empty_sources_list(client):
    """An empty list has no cursors to page, so the worker would spin through MAX_BATCH_ITERATIONS doing nothing
    before exiting — harmless but wasteful; rejected outright instead (bundled minor)."""
    ws = await client.post("/api/workspaces", json={"name": "Empty sources test"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs",
        json={"query": "bert", "filters": {}, "sources": []},
    )

    assert resp.status_code == 422


async def test_unpaywall_is_rejected_as_a_search_source_not_asked_at_all(client):
    """Pins the exact C1 bug the registry closes structurally: "unpaywall" must stay a clean 422 at the schema
    layer, never reach search_batch's _PAGE_FUNCS/PAGE_FUNCS and crash with a KeyError."""
    ws = await client.post("/api/workspaces", json={"name": "Unpaywall not a source test"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["unpaywall"], "query_overrides": {}},
    )

    assert resp.status_code == 422


async def test_start_run_enqueues_the_worker_job(client, arq):
    ws = await client.post("/api/workspaces", json={"name": "Enqueue test"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["arxiv"]},
    )
    run_id = resp.json()["id"]

    assert arq.jobs == [("run_workspace_search", run_id)]


async def test_stop_and_get_run_over_http(client):
    ws = await client.post("/api/workspaces", json={"name": "Stop API test"})
    workspace_id = ws.json()["id"]
    started = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs", json={"query": "bert", "filters": {}, "sources": ["arxiv"]}
    )
    run_id = started.json()["id"]

    stopped = await client.post(f"/api/workspaces/{workspace_id}/search/runs/{run_id}/stop")
    assert stopped.json()["status"] == "stopped"

    fetched = await client.get(f"/api/workspaces/{workspace_id}/search/runs/{run_id}")
    assert fetched.json()["status"] == "stopped"


async def test_restart_run_over_http(client, arq):
    ws = await client.post("/api/workspaces", json={"name": "Restart API test"})
    workspace_id = ws.json()["id"]
    started = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs", json={"query": "bert", "filters": {}, "sources": ["arxiv"]}
    )
    run_id = started.json()["id"]
    await client.post(f"/api/workspaces/{workspace_id}/search/runs/{run_id}/stop")

    restarted = await client.post(f"/api/workspaces/{workspace_id}/search/runs/{run_id}")

    assert restarted.status_code == 200
    assert restarted.json()["status"] == "running"
    assert arq.jobs[-1] == ("run_workspace_search", run_id)


async def test_restart_a_running_run_is_409(client):
    ws = await client.post("/api/workspaces", json={"name": "Conflict API test"})
    workspace_id = ws.json()["id"]
    started = await client.post(
        f"/api/workspaces/{workspace_id}/search/runs", json={"query": "bert", "filters": {}, "sources": ["arxiv"]}
    )
    run_id = started.json()["id"]

    resp = await client.post(f"/api/workspaces/{workspace_id}/search/runs/{run_id}")

    assert resp.status_code == 409


async def test_get_unknown_run_is_404(client):
    ws = await client.post("/api/workspaces", json={"name": "404 test"})
    workspace_id = ws.json()["id"]
    resp = await client.get(f"/api/workspaces/{workspace_id}/search/runs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_start_run_for_unknown_workspace_is_404(client):
    resp = await client.post(
        "/api/workspaces/00000000-0000-0000-0000-000000000000/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["arxiv"]},
    )
    assert resp.status_code == 404


async def test_get_run_for_wrong_workspace_is_404(client):
    owner_ws = await client.post("/api/workspaces", json={"name": "Owner workspace"})
    other_ws = await client.post("/api/workspaces", json={"name": "Other workspace"})
    started = await client.post(
        f"/api/workspaces/{owner_ws.json()['id']}/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["arxiv"]},
    )
    run_id = started.json()["id"]

    resp = await client.get(f"/api/workspaces/{other_ws.json()['id']}/search/runs/{run_id}")

    assert resp.status_code == 404


async def test_stop_run_for_wrong_workspace_is_404(client):
    owner_ws = await client.post("/api/workspaces", json={"name": "Owner workspace"})
    other_ws = await client.post("/api/workspaces", json={"name": "Other workspace"})
    started = await client.post(
        f"/api/workspaces/{owner_ws.json()['id']}/search/runs",
        json={"query": "bert", "filters": {}, "sources": ["arxiv"]},
    )
    run_id = started.json()["id"]

    resp = await client.post(f"/api/workspaces/{other_ws.json()['id']}/search/runs/{run_id}/stop")

    assert resp.status_code == 404

    fetched = await client.get(f"/api/workspaces/{owner_ws.json()['id']}/search/runs/{run_id}")
    assert fetched.json()["status"] == "running"


async def test_list_hits_paginates_by_cursor(session, client):
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models.workspace import Workspace
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    workspace = Workspace(name=f"Hit list {uuid_mod.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    for i in range(5):
        session.add(WorkspaceSearchHit(
            workspace_id=workspace.id, run_id=run.id, source_method="database_search",
            normalized_title=f"paper {i}", first_seen_at=datetime.now(timezone.utc),
        ))
    await session.commit()

    first_page = await client.get(f"/api/workspaces/{workspace.id}/search/hits?limit=2")
    assert len(first_page.json()["items"]) == 2
    cursor = first_page.json()["next_cursor"]
    assert cursor is not None

    second_page = await client.get(f"/api/workspaces/{workspace.id}/search/hits?limit=2&after={cursor}")
    assert len(second_page.json()["items"]) == 2
    first_ids = {h["id"] for h in first_page.json()["items"]}
    second_ids = {h["id"] for h in second_page.json()["items"]}
    assert first_ids.isdisjoint(second_ids)

    # Walk the cursor to exhaustion: every hit must appear exactly once across all pages, confirming the
    # keyset cursor produces no gaps and no overlap end to end, not just between the first two pages.
    seen_ids = [*first_ids, *second_ids]
    next_cursor = second_page.json()["next_cursor"]
    while next_cursor is not None:
        page = await client.get(f"/api/workspaces/{workspace.id}/search/hits?limit=2&after={next_cursor}")
        body = page.json()
        seen_ids.extend(h["id"] for h in body["items"])
        next_cursor = body["next_cursor"]

    assert len(seen_ids) == 5
    assert len(set(seen_ids)) == 5


async def test_list_hits_filters_by_stage1_status(session, client):
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models.workspace import Workspace
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    workspace = Workspace(name=f"Hit filter {uuid_mod.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search",
        normalized_title="relevant one", stage1_status="relevant", first_seen_at=datetime.now(timezone.utc),
    ))
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search",
        normalized_title="excluded one", stage1_status="not_relevant", first_seen_at=datetime.now(timezone.utc),
    ))
    await session.commit()

    resp = await client.get(f"/api/workspaces/{workspace.id}/search/hits?stage1_status=relevant")
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["normalized_title"] == "relevant one"


async def test_list_hits_filters_by_acquisition_status(session, client):
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models.workspace import Workspace
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    workspace = Workspace(name=f"Hit acquisition filter {uuid_mod.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search",
        normalized_title="failed one", acquisition_status="failed", first_seen_at=datetime.now(timezone.utc),
    ))
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search",
        normalized_title="not attempted one", first_seen_at=datetime.now(timezone.utc),
    ))
    await session.commit()

    resp = await client.get(f"/api/workspaces/{workspace.id}/search/hits?acquisition_status=failed")
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["normalized_title"] == "failed one"


async def test_list_hits_surfaces_the_linked_external_ref_and_tolerates_none(session, client):
    """I7: HitOut's title/authors/year/venue/doi/abstract come from a join to ExternalRef, not just
    normalized_title. A hit with no external_ref_id (never matched to a candidate, or the match was cleared) must
    still come back with those fields None instead of crashing the join."""
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models.references import ExternalRef
    from app.models.workspace import Workspace
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    workspace = Workspace(name=f"Hit ref join {uuid_mod.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    ref = ExternalRef(
        title="The Real Title", authors=["Ada Lovelace"], year=1843, venue="Analytical Engine Quarterly",
        doi="10.1234/real", abstract="A study of the Analytical Engine's general applicability.",
        sources=["arxiv", "openalex"],
    )
    session.add(ref)
    await session.flush()
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search", external_ref_id=ref.id,
        normalized_title="the real title", first_seen_at=datetime.now(timezone.utc),
    ))
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search", external_ref_id=None,
        normalized_title="no ref one", first_seen_at=datetime.now(timezone.utc),
    ))
    await session.commit()

    resp = await client.get(f"/api/workspaces/{workspace.id}/search/hits")

    assert resp.status_code == 200
    items = {item["normalized_title"]: item for item in resp.json()["items"]}
    linked = items["the real title"]
    assert linked["title"] == "The Real Title"
    assert linked["authors"] == ["Ada Lovelace"]
    assert linked["year"] == 1843
    assert linked["venue"] == "Analytical Engine Quarterly"
    assert linked["doi"] == "10.1234/real"
    assert linked["abstract"] == "A study of the Analytical Engine's general applicability."
    assert linked["sources"] == ["arxiv", "openalex"]
    unlinked = items["no ref one"]
    assert unlinked["title"] is None
    assert unlinked["authors"] is None
    assert unlinked["year"] is None
    assert unlinked["venue"] is None
    assert unlinked["doi"] is None
    assert unlinked["abstract"] is None
    assert unlinked["sources"] == []


async def test_list_hits_with_zero_limit_is_422(client):
    ws = await client.post("/api/workspaces", json={"name": "Zero limit test"})
    workspace_id = ws.json()["id"]

    resp = await client.get(f"/api/workspaces/{workspace_id}/search/hits?limit=0")

    assert resp.status_code == 422


async def test_list_hits_with_a_garbage_stage1_status_filter_is_422(client):
    """stage1_status/acquisition_status used to be plain str query params — a garbage value silently matched
    nothing instead of a clean 422 (bundled minor)."""
    ws = await client.post("/api/workspaces", json={"name": "Garbage filter test"})
    workspace_id = ws.json()["id"]

    resp = await client.get(f"/api/workspaces/{workspace_id}/search/hits?stage1_status=not-a-real-status")

    assert resp.status_code == 422


async def test_list_hits_with_malformed_cursor_is_422(client):
    ws = await client.post("/api/workspaces", json={"name": "Bad cursor test"})
    workspace_id = ws.json()["id"]

    resp = await client.get(f"/api/workspaces/{workspace_id}/search/hits?after=not-valid-base64-json")

    assert resp.status_code == 422


async def _make_hit(session, title: str, workspace_id=None):
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models.workspace import Workspace
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    if workspace_id is None:
        workspace = Workspace(name=f"Patch {uuid_mod.uuid4().hex[:8]}")
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
    run = WorkspaceSearchRun(
        workspace_id=workspace_id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit = WorkspaceSearchHit(
        workspace_id=workspace_id, run_id=run.id, source_method="database_search",
        normalized_title=title, first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit)
    await session.commit()
    return hit.id


async def test_patch_hit_sets_stage1_fields(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "patch target")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}",
        json={"stage1_status": "relevant", "priority": 4, "topic_fit": "same_topic"},
    )

    assert resp.status_code == 200
    assert resp.json()["stage1_status"] == "relevant"
    assert resp.json()["priority"] == 4


async def test_patch_hit_exclude_requires_a_reason(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "exclude target")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}", json={"stage1_status": "not_relevant"}
    )

    assert resp.status_code == 422


async def test_patch_hit_exclude_with_a_reason_succeeds(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "exclude target 2")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}",
        json={"stage1_status": "not_relevant", "stage1_exclude_reason": "wrong_topic"},
    )

    assert resp.status_code == 200
    assert resp.json()["stage1_status"] == "not_relevant"
    assert resp.json()["stage1_exclude_reason"] == "wrong_topic"


async def test_patch_hit_cannot_null_out_reason_while_still_not_relevant(session, client):
    """A second PATCH that omits stage1_status entirely must not be able to null the reason out from under an
    already-not_relevant hit — the schema validator alone can't catch this since it never sees the hit's
    persisted state, only this request's own fields."""
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "reason bypass target")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    first = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}",
        json={"stage1_status": "not_relevant", "stage1_exclude_reason": "wrong_topic"},
    )
    assert first.status_code == 200

    second = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}",
        json={"stage1_exclude_reason": None},
    )

    assert second.status_code == 422
    hit = await session.get(WorkspaceSearchHit, hit_id)
    assert hit.stage1_status == "not_relevant"
    assert hit.stage1_exclude_reason == "wrong_topic"


async def test_patch_hit_rejects_a_garbage_stage1_status(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "garbage status")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}", json={"stage1_status": "super_relevant"}
    )

    assert resp.status_code == 422


async def test_patch_hit_rejects_priority_out_of_range(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "bad priority")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}", json={"priority": 9}
    )

    assert resp.status_code == 422


async def test_patch_hit_for_unknown_hit_is_404(client):
    ws = await client.post("/api/workspaces", json={"name": "Patch 404 test"})
    workspace_id = ws.json()["id"]

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/00000000-0000-0000-0000-000000000000",
        json={"stage1_status": "relevant"},
    )

    assert resp.status_code == 404


async def test_patch_hit_for_wrong_workspace_is_404(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_hit(session, "wrong workspace target")
    other_ws = await client.post("/api/workspaces", json={"name": "Other patch workspace"})
    other_workspace_id = other_ws.json()["id"]

    resp = await client.patch(
        f"/api/workspaces/{other_workspace_id}/search/hits/{hit_id}", json={"stage1_status": "relevant"}
    )

    assert resp.status_code == 404
    hit = await session.get(WorkspaceSearchHit, hit_id)
    assert hit.stage1_status is None


async def test_bulk_patch_hits(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_a = await _make_hit(session, "bulk a")
    workspace_id = (await session.get(WorkspaceSearchHit, hit_a)).workspace_id
    hit_b = await _make_hit(session, "bulk b", workspace_id=workspace_id)

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/bulk",
        json={"hit_ids": [str(hit_a), str(hit_b)], "stage1_status": "relevant"},
    )

    assert resp.status_code == 200
    assert resp.json()["updated"] == 2


async def test_bulk_patch_hits_exclude_requires_a_reason(session, client):
    hit_a = await _make_hit(session, "bulk exclude a")
    from app.models.workspace_search import WorkspaceSearchHit

    workspace_id = (await session.get(WorkspaceSearchHit, hit_a)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/bulk",
        json={"hit_ids": [str(hit_a)], "stage1_status": "not_relevant"},
    )

    assert resp.status_code == 422


async def test_bulk_patch_hits_rejects_a_garbage_stage1_status(session, client):
    hit_a = await _make_hit(session, "bulk garbage a")
    from app.models.workspace_search import WorkspaceSearchHit

    workspace_id = (await session.get(WorkspaceSearchHit, hit_a)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/bulk",
        json={"hit_ids": [str(hit_a)], "stage1_status": "super_relevant"},
    )

    assert resp.status_code == 422


async def test_bulk_patch_hits_only_updates_hits_in_the_workspace(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    owned_hit = await _make_hit(session, "owned bulk hit")
    other_hit = await _make_hit(session, "other workspace bulk hit")
    workspace_id = (await session.get(WorkspaceSearchHit, owned_hit)).workspace_id

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/search/hits/bulk",
        json={"hit_ids": [str(owned_hit), str(other_hit)], "stage1_status": "relevant"},
    )

    assert resp.status_code == 200
    assert resp.json()["updated"] == 1
    other = await session.get(WorkspaceSearchHit, other_hit)
    assert other.stage1_status is None


async def _make_importable_hit(
    session, pdf_urls: list[str], workspace_id=None, imported_as=None,
    source_method="database_search", seed_paper_id=None,
):
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models.references import ExternalRef
    from app.models.workspace import Workspace
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    if workspace_id is None:
        workspace = Workspace(name=f"Import {uuid_mod.uuid4().hex[:8]}")
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
    ref = ExternalRef(title="Importable Paper", pdf_urls=pdf_urls, imported_as=imported_as)
    session.add(ref)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace_id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit = WorkspaceSearchHit(
        workspace_id=workspace_id, run_id=run.id, external_ref_id=ref.id, source_method=source_method,
        seed_paper_id=seed_paper_id,
        normalized_title="importable paper", stage1_status="relevant", first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit)
    await session.commit()
    return hit.id


async def test_import_hits_with_free_pdf_succeeds_and_enqueues_ingest(session, client, discovery_api, arq):
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/paper.pdf", 200, content=b"%PDF-1.4 fake content")
    hit_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/paper.pdf"])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    workspace_id = hit.workspace_id

    resp = await client.post(f"/api/workspaces/{workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}

    await session.refresh(hit)
    assert hit.acquisition_status == "imported"
    assert hit.paper_id is not None
    assert arq.jobs == [("ingest_paper", str(hit.paper_id))]


async def test_import_hits_with_no_pdf_urls_skips_straight_to_failed(session, client, discovery_api):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_importable_hit(session, pdf_urls=[])
    hit = await session.get(WorkspaceSearchHit, hit_id)

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 0, "failed": 1}
    assert discovery_api.pdf_host.requests == []  # download_pdf must never be called with an empty url list

    await session.refresh(hit)
    assert hit.acquisition_status == "failed"
    assert hit.paper_id is None


async def test_import_hits_download_failure_marks_failed_and_continues_the_batch(session, client, discovery_api):
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/gone.pdf", 404, text="not found")
    discovery_api.pdf_host.reply("/ok.pdf", 200, content=b"%PDF-1.4 ok content")

    failing_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/gone.pdf"])
    workspace_id = (await session.get(WorkspaceSearchHit, failing_id)).workspace_id
    ok_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/ok.pdf"], workspace_id=workspace_id)

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/hits/import",
        json={"hit_ids": [str(failing_id), str(ok_id)]},
    )

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 1}

    failing_hit = await session.get(WorkspaceSearchHit, failing_id)
    ok_hit = await session.get(WorkspaceSearchHit, ok_id)
    await session.refresh(failing_hit)
    await session.refresh(ok_hit)
    assert failing_hit.acquisition_status == "failed"
    assert ok_hit.acquisition_status == "imported"
    assert ok_hit.paper_id is not None


async def test_import_hits_only_imports_hits_owned_by_the_workspace(session, client, discovery_api):
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/other.pdf", 200, content=b"%PDF-1.4 other content")
    owned_id = await _make_importable_hit(session, pdf_urls=[])
    owned_hit = await session.get(WorkspaceSearchHit, owned_id)
    other_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/other.pdf"])

    resp = await client.post(
        f"/api/workspaces/{owned_hit.workspace_id}/search/hits/import",
        json={"hit_ids": [str(owned_id), str(other_id)]},
    )

    assert resp.status_code == 200
    assert resp.json() == {"imported": 0, "failed": 1}  # only the owned (no-pdf) hit is processed
    other_hit = await session.get(WorkspaceSearchHit, other_id)
    assert other_hit.acquisition_status == "not_attempted"
    assert discovery_api.pdf_host.requests == []


async def test_import_hits_without_hit_ids_targets_relevant_not_yet_imported_hits(session, client, discovery_api):
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/new.pdf", 200, content=b"%PDF-1.4 new content")
    new_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/new.pdf"])
    workspace_id = (await session.get(WorkspaceSearchHit, new_id)).workspace_id
    already_id = await _make_importable_hit(session, pdf_urls=[], workspace_id=workspace_id)
    already_hit = await session.get(WorkspaceSearchHit, already_id)
    already_hit.acquisition_status = "imported"
    await session.commit()

    resp = await client.post(f"/api/workspaces/{workspace_id}/search/hits/import", json={})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}
    new_hit = await session.get(WorkspaceSearchHit, new_id)
    await session.refresh(new_hit)
    assert new_hit.acquisition_status == "imported"


async def test_import_hits_already_in_library_attaches_existing_paper_without_downloading(
    session, client, discovery_api, arq
):
    """The reference behind this hit was already imported — an earlier run, or the References panel's own
    import_reference flow — so ExternalRef.imported_as is already set. import_hits must not download or create
    a duplicate paper for it: it attaches the existing paper and skips straight to imported."""
    from app.models import Paper
    from app.models.workspace_search import WorkspaceSearchHit

    existing_paper = Paper(title="Already in the library", file_path="/nonexistent.pdf")
    session.add(existing_paper)
    await session.flush()

    hit_id = await _make_importable_hit(
        session, pdf_urls=["https://pdf.example/should-not-be-fetched.pdf"], imported_as=existing_paper.id
    )
    hit = await session.get(WorkspaceSearchHit, hit_id)
    workspace_id = hit.workspace_id

    resp = await client.post(f"/api/workspaces/{workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}
    assert discovery_api.pdf_host.requests == []  # no download attempted

    await session.refresh(hit)
    assert (hit.acquisition_status, hit.paper_id) == ("imported", existing_paper.id)
    assert arq.jobs == []  # ingest_paper must not be enqueued again for a paper that already exists

    from sqlalchemy import select

    from app.models import workspace_papers

    membership = await session.execute(
        select(workspace_papers).where(
            workspace_papers.c.workspace_id == workspace_id, workspace_papers.c.paper_id == existing_paper.id
        )
    )
    assert membership.first() is not None  # workspaces.add_paper ran for the existing paper


async def test_import_hits_new_import_sets_external_ref_imported_as(session, client, discovery_api):
    """The same bookkeeping import_reference does: once a hit's download succeeds and creates a new paper, the
    backing ExternalRef.imported_as must point at it, so other papers' References tabs citing this reference (or
    the References panel's own import) see it as already in the library instead of importing it again."""
    from app.models.references import ExternalRef
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/fresh.pdf", 200, content=b"%PDF-1.4 fresh content")
    hit_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/fresh.pdf"])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    workspace_id = hit.workspace_id
    ref_id = hit.external_ref_id

    resp = await client.post(f"/api/workspaces/{workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}

    await session.refresh(hit)
    ref = await session.get(ExternalRef, ref_id)
    await session.refresh(ref)
    assert ref.imported_as == hit.paper_id


async def test_import_hits_from_snowball_backward_records_a_cites_edge_in_the_graph(
    session, client, discovery_api
):
    """Owner's choice (2026-10-04): once a snowball-discovered hit is imported, the existing Graph picks up the
    hop as a citation edge — no separate "snowball graph" view needed. Backward = the seed's own references, so
    the seed cites the newly imported paper."""
    from app.core import graph, workspaces
    from app.models import Paper
    from app.models.workspace_search import WorkspaceSearchHit

    seed = Paper(title="Backward Seed", file_path="/nonexistent.pdf")
    session.add(seed)
    await session.flush()
    discovery_api.pdf_host.reply("/backward.pdf", 200, content=b"%PDF-1.4 backward content")
    hit_id = await _make_importable_hit(
        session, pdf_urls=["https://pdf.example/backward.pdf"],
        source_method="snowball_backward", seed_paper_id=seed.id,
    )
    hit = await session.get(WorkspaceSearchHit, hit_id)
    await workspaces.add_paper(session, hit.workspace_id, seed.id)

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}
    await session.refresh(hit)

    result = await graph.library_graph(session)
    assert any(
        link.kind == "cites" and link.source == seed.id and link.target == hit.paper_id for link in result.links
    )


async def test_import_hits_from_snowball_forward_records_a_cited_by_edge(session, client, discovery_api):
    """Forward = works that cite the seed, so the newly imported paper cites the seed — recorded from the seed's
    own side as `cited_by`, same convention references.py's own fetch uses."""
    from sqlalchemy import select

    from app.models import Paper
    from app.models.references import paper_references
    from app.models.workspace_search import WorkspaceSearchHit

    seed = Paper(title="Forward Seed", file_path="/nonexistent.pdf")
    session.add(seed)
    await session.flush()
    discovery_api.pdf_host.reply("/forward.pdf", 200, content=b"%PDF-1.4 forward content")
    hit_id = await _make_importable_hit(
        session, pdf_urls=["https://pdf.example/forward.pdf"],
        source_method="snowball_forward", seed_paper_id=seed.id,
    )
    hit = await session.get(WorkspaceSearchHit, hit_id)

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    await session.refresh(hit)
    row = (
        await session.execute(
            select(paper_references).where(
                paper_references.c.paper_id == seed.id, paper_references.c.ref_id == hit.external_ref_id
            )
        )
    ).first()
    assert row is not None and row.direction == "cited_by"


async def test_import_hits_already_imported_snowball_hit_still_records_the_citation_edge(
    session, client, discovery_api
):
    """The already-in-library branch (ExternalRef.imported_as already set) must also record the edge — a seed's
    reference can already be in the library from an earlier import."""
    from sqlalchemy import select

    from app.models import Paper
    from app.models.references import paper_references
    from app.models.workspace_search import WorkspaceSearchHit

    seed = Paper(title="Already-imported-edge seed", file_path="/nonexistent.pdf")
    existing = Paper(title="Already in the library", file_path="/nonexistent.pdf")
    session.add_all([seed, existing])
    await session.flush()
    hit_id = await _make_importable_hit(
        session, pdf_urls=["https://pdf.example/should-not-be-fetched.pdf"], imported_as=existing.id,
        source_method="snowball_backward", seed_paper_id=seed.id,
    )
    hit = await session.get(WorkspaceSearchHit, hit_id)

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}
    row = (
        await session.execute(
            select(paper_references).where(
                paper_references.c.paper_id == seed.id, paper_references.c.ref_id == hit.external_ref_id
            )
        )
    ).first()
    assert row is not None and row.direction == "cites"


async def test_import_hits_non_snowball_hit_records_no_citation_edge(session, client, discovery_api):
    """A database-search hit never gets a citation edge, even if it happens to carry a seed_paper_id — the check
    is on source_method, not merely on seed_paper_id being set."""
    from sqlalchemy import select

    from app.models import Paper
    from app.models.references import paper_references

    seed = Paper(title="Not a snowball seed", file_path="/nonexistent.pdf")
    session.add(seed)
    await session.flush()
    discovery_api.pdf_host.reply("/plain.pdf", 200, content=b"%PDF-1.4 plain content")
    hit_id = await _make_importable_hit(
        session, pdf_urls=["https://pdf.example/plain.pdf"],
        source_method="database_search", seed_paper_id=seed.id,
    )
    from app.models.workspace_search import WorkspaceSearchHit

    hit = await session.get(WorkspaceSearchHit, hit_id)

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    rows = (await session.execute(select(paper_references).where(paper_references.c.paper_id == seed.id))).all()
    assert rows == []


async def test_import_hits_skips_a_manually_acquired_hit(session, client, discovery_api):
    """Running "Import all" after a hit was manually uploaded must not re-attach it and overwrite its provenance
    from "manual" to "imported" — spec §4.4 and M30b's PRISMA export need that distinction kept (I8)."""
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/should-not-be-fetched.pdf", 200, content=b"%PDF-1.4 should not be used")
    hit_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/should-not-be-fetched.pdf"])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    hit.acquisition_status = "manual"
    await session.commit()

    resp = await client.post(
        f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]}
    )

    assert resp.status_code == 200
    assert resp.json() == {"imported": 0, "failed": 0}  # skipped entirely — neither imported nor failed
    assert discovery_api.pdf_host.requests == []  # never re-downloaded

    await session.refresh(hit)
    assert hit.acquisition_status == "manual"


async def test_import_hits_enriches_a_missing_pdf_via_unpaywall_before_failing(session, client, app, discovery_fakes):
    """spec §6: Unpaywall is DOI-only PDF enrichment for a candidate missing a free PDF, applied post-merge —
    import_hits only ever checked ref.pdf_urls as already stored and never called into it (I5)."""
    from app.models.references import ExternalRef
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_importable_hit(session, pdf_urls=[])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    workspace_id = hit.workspace_id
    ref = await session.get(ExternalRef, hit.external_ref_id)
    ref.doi = "10.5555/paperlab-i5-unpaywall"
    await session.commit()

    discovery_fakes.unpaywall.reply(
        f"/v2/{ref.doi}", 200,
        json={"best_oa_location": {"url_for_pdf": "https://pdf.example/i5.pdf"}, "oa_locations": []},
    )
    discovery_fakes.pdf_host.reply("/i5.pdf", 200, content=b"%PDF-1.4 enriched content")
    app.dependency_overrides[get_discovery] = lambda: discovery_fakes.turned_on("unpaywall")

    resp = await client.post(f"/api/workspaces/{workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "failed": 0}

    await session.refresh(hit)
    assert hit.acquisition_status == "imported"
    await session.refresh(ref)
    assert ref.pdf_urls == ["https://pdf.example/i5.pdf"]


async def test_upload_pdf_marks_hit_manual_and_enqueues_ingest(session, client, arq):
    from app.models.references import ExternalRef
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_importable_hit(session, pdf_urls=[])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    workspace_id = hit.workspace_id
    ref_id = hit.external_ref_id

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}/upload",
        files={"file": ("paper.pdf", b"%PDF-1.4 real enough for the check", "application/pdf")},
    )

    assert resp.status_code == 200
    await session.refresh(hit)
    assert hit.acquisition_status == "manual"
    assert hit.paper_id is not None
    assert arq.jobs == [("ingest_paper", str(hit.paper_id))]

    ref = await session.get(ExternalRef, ref_id)
    await session.refresh(ref)
    assert ref.imported_as == hit.paper_id  # same bookkeeping as import_hits' new-import branch


async def test_upload_non_pdf_is_rejected(session, client, arq):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_importable_hit(session, pdf_urls=[])
    workspace_id = (await session.get(WorkspaceSearchHit, hit_id)).workspace_id

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}/upload",
        files={"file": ("paper.pdf", b"<html>not a pdf</html>", "application/pdf")},
    )

    assert resp.status_code == 422
    hit = await session.get(WorkspaceSearchHit, hit_id)
    await session.refresh(hit)
    assert hit.acquisition_status != "manual"
    assert hit.paper_id is None
    assert arq.jobs == []


async def test_upload_for_already_imported_hit_attaches_existing_paper(session, client, arq):
    """The reference behind this hit is already imported (an earlier hit, run, or the References panel). Uploading
    a file for it must attach the existing paper, not create a duplicate or re-enqueue ingest for it."""
    from app.models import Paper
    from app.models.workspace_search import WorkspaceSearchHit

    existing_paper = Paper(title="Already in the library", file_path="/nonexistent.pdf")
    session.add(existing_paper)
    await session.flush()

    hit_id = await _make_importable_hit(session, pdf_urls=[], imported_as=existing_paper.id)
    hit = await session.get(WorkspaceSearchHit, hit_id)
    workspace_id = hit.workspace_id

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/hits/{hit_id}/upload",
        files={"file": ("paper.pdf", b"%PDF-1.4 ignored content", "application/pdf")},
    )

    assert resp.status_code == 200
    await session.refresh(hit)
    assert (hit.acquisition_status, hit.paper_id) == ("manual", existing_paper.id)
    assert arq.jobs == []  # no new paper created, so no ingest job

    from sqlalchemy import select

    from app.models import workspace_papers

    membership = await session.execute(
        select(workspace_papers).where(
            workspace_papers.c.workspace_id == workspace_id, workspace_papers.c.paper_id == existing_paper.id
        )
    )
    assert membership.first() is not None


async def test_upload_for_wrong_workspace_is_404(session, client):
    from app.models.workspace_search import WorkspaceSearchHit

    hit_id = await _make_importable_hit(session, pdf_urls=[])
    other_ws = await client.post("/api/workspaces", json={"name": "Other upload workspace"})
    other_workspace_id = other_ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{other_workspace_id}/search/hits/{hit_id}/upload",
        files={"file": ("paper.pdf", b"%PDF-1.4 content", "application/pdf")},
    )

    assert resp.status_code == 404
    hit = await session.get(WorkspaceSearchHit, hit_id)
    await session.refresh(hit)
    assert hit.acquisition_status != "manual"


async def test_clear_hits_deletes_everything_not_imported_or_manual(session, client):
    from sqlalchemy import select

    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    ws = await client.post("/api/workspaces", json={"name": "Clear hits test"})
    workspace_id = ws.json()["id"]
    run = WorkspaceSearchRun(
        workspace_id=workspace_id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()

    def hit(status: str, **extra) -> WorkspaceSearchHit:
        return WorkspaceSearchHit(
            workspace_id=workspace_id, run_id=run.id, source_method="database_search",
            normalized_title=f"{status} hit {uuid.uuid4().hex[:6]}", first_seen_at=datetime.now(timezone.utc),
            acquisition_status=status, **extra,
        )

    not_attempted = hit("not_attempted")
    failed = hit("failed")
    imported = hit("imported")
    manual = hit("manual")
    session.add_all([not_attempted, failed, imported, manual])
    await session.commit()

    resp = await client.request("DELETE", f"/api/workspaces/{workspace_id}/search/hits")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    remaining = (
        await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == workspace_id))
    ).scalars().all()
    assert {h.id for h in remaining} == {imported.id, manual.id}


async def test_clear_hits_for_unknown_workspace_is_404(client):
    resp = await client.request("DELETE", f"/api/workspaces/{uuid.uuid4()}/search/hits")

    assert resp.status_code == 404


async def test_snowball_route_stores_hits_and_returns_counts(session, client, fake_providers_all):
    """POST /search/snowball with a seed paper in the workspace returns 200 with new_hits > 0."""
    from app.models import Paper
    from app.core import workspaces

    # Create workspace
    ws = await client.post("/api/workspaces", json={"name": "Snowball test"})
    workspace_id = ws.json()["id"]

    # Create and add a seed paper that the fake S2 provider has references for
    # doi="10.5555/paperlab-e2e-free" is one of the discovery_fake.PAPERS fixtures
    seed = Paper(
        title="PaperLab Find Papers Fixture", doi="10.5555/paperlab-e2e-free", file_path="/nonexistent.pdf"
    )
    session.add(seed)
    await session.flush()
    await workspaces.add_paper(session, workspace_id, seed.id)

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/snowball",
        json={"seed_paper_ids": [str(seed.id)], "backward": True, "forward": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["new_hits"] > 0
    assert body["skipped_seeds"] == []
    assert body["errors"] == {}


async def test_snowball_route_with_unowned_seed_is_404(session, client, fake_providers_all):
    """POST /search/snowball with a seed_paper_ids entry that isn't in the workspace returns 404."""
    from app.models import Paper
    from app.core import workspaces

    # Create two workspaces
    ws1 = await client.post("/api/workspaces", json={"name": "Workspace 1"})
    workspace_id1 = ws1.json()["id"]
    ws2 = await client.post("/api/workspaces", json={"name": "Workspace 2"})
    workspace_id2 = ws2.json()["id"]

    # Create a seed paper in workspace 1
    seed = Paper(title="Test Paper", doi="10.5555/test", file_path="/nonexistent.pdf")
    session.add(seed)
    await session.flush()
    await workspaces.add_paper(session, workspace_id1, seed.id)

    # Try to snowball on workspace 2 with workspace 1's seed
    resp = await client.post(
        f"/api/workspaces/{workspace_id2}/search/snowball",
        json={"seed_paper_ids": [str(seed.id)], "backward": True, "forward": False},
    )

    assert resp.status_code == 404


async def test_snowball_route_rejects_an_empty_seed_paper_ids_list(client):
    """An empty seed list has nothing to hop from — SnowballRequest.seed_paper_ids rejects it with a 422 instead
    of accepting a request that can only ever produce zero results."""
    ws = await client.post("/api/workspaces", json={"name": f"Snowball empty seeds {uuid.uuid4().hex[:8]}"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/snowball",
        json={"seed_paper_ids": [], "backward": True, "forward": False},
    )

    assert resp.status_code == 422


async def test_snowball_route_with_seed_hit_ids_only_returns_200(session, client, fake_providers_all):
    """POST /search/snowball with only seed_hit_ids (no seed_paper_ids) returns 200 with new_hits > 0 — the one
    test that actually exercises the route's positional wiring of payload.seed_hit_ids through to
    workspace_search.snowball, as opposed to the engine-level tests that call snowball() directly."""
    from app.models.references import ExternalRef
    from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

    ws = await client.post("/api/workspaces", json={"name": f"Snowball hit seed route test {uuid.uuid4().hex[:8]}"})
    workspace_id = ws.json()["id"]

    free_title, free_doi, _ = discovery_fake.PAPERS[0]
    ref = ExternalRef(title=free_title, doi=free_doi)
    session.add(ref)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=uuid.UUID(workspace_id), query_text="prior database search", sources_json=["arxiv"],
        status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    hit = WorkspaceSearchHit(
        workspace_id=uuid.UUID(workspace_id), run_id=run.id, external_ref_id=ref.id, source_method="database_search",
        normalized_title=free_title.lower(), first_seen_at=datetime.now(timezone.utc),
    )
    session.add(hit)
    await session.commit()

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/snowball",
        json={"seed_hit_ids": [str(hit.id)], "backward": True, "forward": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["new_hits"] > 0
    assert body["skipped_seeds"] == []
    assert body["errors"] == {}


async def test_snowball_route_rejects_when_both_seed_lists_are_empty(client):
    """Neither seed_paper_ids nor seed_hit_ids has anything to hop from — the model_validator added for
    seed_hit_ids rejects this combination with a 422, same convention as the paper-only case above."""
    ws = await client.post("/api/workspaces", json={"name": f"Snowball both empty {uuid.uuid4().hex[:8]}"})
    workspace_id = ws.json()["id"]

    resp = await client.post(
        f"/api/workspaces/{workspace_id}/search/snowball",
        json={"seed_paper_ids": [], "seed_hit_ids": [], "backward": True, "forward": False},
    )

    assert resp.status_code == 422


async def test_list_hits_surfaces_stage2_eligibility(session, client):
    """A hit whose paper has a stage-2 eligibility verdict for its own run shows stage2_status/
    stage2_exclude_reason in the GET /search/hits response; a hit with no verdict yet shows both as null."""
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from app.models import Paper
    from app.models.workspace import Workspace
    from app.models.workspace_search import SearchRunEligibility, WorkspaceSearchHit, WorkspaceSearchRun

    workspace = Workspace(name=f"Eligibility join {uuid_mod.uuid4().hex[:8]}")
    session.add(workspace)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    paper = Paper(title="Eligible Paper", doi=f"10.9999/{uuid_mod.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search", paper_id=paper.id,
        normalized_title="assessed hit", first_seen_at=datetime.now(timezone.utc),
    ))
    session.add(WorkspaceSearchHit(
        workspace_id=workspace.id, run_id=run.id, source_method="database_search",
        normalized_title="unassessed hit", first_seen_at=datetime.now(timezone.utc),
    ))
    session.add(SearchRunEligibility(
        paper_id=paper.id, search_run_id=run.id, stage2_status="include", assessed_at=datetime.now(timezone.utc),
    ))
    await session.commit()

    resp = await client.get(f"/api/workspaces/{workspace.id}/search/hits")

    assert resp.status_code == 200
    items = {item["normalized_title"]: item for item in resp.json()["items"]}
    assert items["assessed hit"]["stage2_status"] == "include"
    assert items["assessed hit"]["stage2_exclude_reason"] is None
    assert items["unassessed hit"]["stage2_status"] is None
    assert items["unassessed hit"]["stage2_exclude_reason"] is None


async def _make_eligible_paper(session, client, workspace_id=None):
    """Workspace (if not given) + a paper that's a member of it + an exhausted run in that same workspace —
    the minimum an eligibility PATCH needs to succeed. Returns (workspace_id, paper_id, run_id)."""
    from app.core import workspaces
    from app.models import Paper
    from app.models.workspace_search import WorkspaceSearchRun

    if workspace_id is None:
        ws = await client.post("/api/workspaces", json={"name": f"Eligibility route {uuid.uuid4().hex[:8]}"})
        workspace_id = ws.json()["id"]
    paper = Paper(title="Route Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    await workspaces.add_paper(session, workspace_id, paper.id)
    run = WorkspaceSearchRun(
        workspace_id=workspace_id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    await session.commit()
    return workspace_id, paper.id, run.id


async def test_set_eligibility_route_sets_and_upserts(session, client):
    workspace_id, paper_id, run_id = await _make_eligible_paper(session, client)

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/papers/{paper_id}/eligibility?run={run_id}",
        json={"status": "include"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["paper_id"] == str(paper_id)
    assert body["search_run_id"] == str(run_id)
    assert body["stage2_status"] == "include"
    assert body["stage2_exclude_reason"] is None

    resp2 = await client.patch(
        f"/api/workspaces/{workspace_id}/papers/{paper_id}/eligibility?run={run_id}",
        json={"status": "exclude", "exclude_reason": "not_relevant"},
    )

    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["stage2_status"] == "exclude"
    assert body2["stage2_exclude_reason"] == "not_relevant"


async def test_set_eligibility_route_for_paper_not_in_workspace_is_404(session, client):
    from app.models import Paper
    from app.models.workspace_search import WorkspaceSearchRun

    ws = await client.post("/api/workspaces", json={"name": f"Elig no membership {uuid.uuid4().hex[:8]}"})
    workspace_id = ws.json()["id"]
    paper = Paper(title="Outside Paper", doi=f"10.9999/{uuid.uuid4().hex[:8]}", file_path="/nonexistent.pdf")
    session.add(paper)
    await session.flush()
    run = WorkspaceSearchRun(
        workspace_id=workspace_id, query_text="q", filters_json={}, query_overrides_json={},
        sources_json=[], status="exhausted", started_at=datetime.now(timezone.utc), stats_json={},
    )
    session.add(run)
    await session.flush()
    await session.commit()
    # paper deliberately never added to the workspace via workspaces.add_paper

    resp = await client.patch(
        f"/api/workspaces/{workspace_id}/papers/{paper.id}/eligibility?run={run.id}",
        json={"status": "include"},
    )

    assert resp.status_code == 404


async def test_set_eligibility_route_for_run_from_a_different_workspace_is_404(session, client):
    workspace_id1, paper_id1, run_id1 = await _make_eligible_paper(session, client)
    ws2 = await client.post("/api/workspaces", json={"name": f"Elig other run {uuid.uuid4().hex[:8]}"})
    workspace_id2 = ws2.json()["id"]

    resp = await client.patch(
        f"/api/workspaces/{workspace_id2}/papers/{paper_id1}/eligibility?run={run_id1}",
        json={"status": "include"},
    )

    assert resp.status_code == 404


async def test_prisma_export_route_combined_view(client):
    """GET /search/prisma?runs=all with no runs yet: every count is 0, runs=[]."""
    ws = await client.post("/api/workspaces", json={"name": "PRISMA combined test"})
    workspace_id = ws.json()["id"]

    resp = await client.get(f"/api/workspaces/{workspace_id}/search/prisma?runs=all")

    assert resp.status_code == 200
    body = resp.json()
    assert body["identified"] == 0
    assert body["duplicates_removed"] == 0
    assert body["stage1_screened"] == 0
    assert body["stage1_excluded"] == 0
    assert body["stage1_excluded_by_reason"] == {}
    assert body["sought"] == 0
    assert body["not_retrieved"] == 0
    assert body["stage2_assessed"] == 0
    assert body["stage2_excluded"] == 0
    assert body["stage2_excluded_by_reason"] == {}
    assert body["included"] == 0
    assert body["runs"] == []


async def test_prisma_export_route_rejects_a_run_id_from_another_workspace(client):
    """Ownership check: a run_id that exists but belongs to a different workspace is 404."""
    owner_ws = await client.post("/api/workspaces", json={"name": "PRISMA owner workspace"})
    owner_ws_id = owner_ws.json()["id"]
    other_ws = await client.post("/api/workspaces", json={"name": "PRISMA other workspace"})
    other_ws_id = other_ws.json()["id"]

    started = await client.post(
        f"/api/workspaces/{owner_ws_id}/search/runs",
        json={"query": "test", "filters": {}, "sources": ["arxiv"]},
    )
    run_id = started.json()["id"]

    resp = await client.get(f"/api/workspaces/{other_ws_id}/search/prisma?runs={run_id}")

    assert resp.status_code == 404


async def test_prisma_export_route_rejects_malformed_runs_value(client):
    """Malformed runs value (not 'all' and not a valid UUID) should be a clean 422 with appropriate error message."""
    ws = await client.post("/api/workspaces", json={"name": "PRISMA malformed test"})
    workspace_id = ws.json()["id"]

    resp = await client.get(f"/api/workspaces/{workspace_id}/search/prisma?runs=not-a-uuid")

    assert resp.status_code == 422
    body = resp.json()
    assert "invalid runs value" in body["detail"].lower()


async def test_start_run_stores_a_year_range_and_rejects_other_filters(client):
    ws = await client.post("/api/workspaces", json={"name": "Search years API"})
    url = f"/api/workspaces/{ws.json()['id']}/search/runs"

    ok = await client.post(url, json={"query": "bert", "filters": {"year_from": 2023}, "sources": ["arxiv"]})
    bad = await client.post(url, json={"query": "bert", "filters": {"bogus": 1}, "sources": ["arxiv"]})

    assert ok.status_code == 201 and ok.json()["filters_json"] == {"year_from": 2023}
    assert bad.status_code == 422


async def test_import_hits_keeps_the_hits_bibliographic_metadata_and_locks_title_and_doi(
    session, client, discovery_api
):
    """Bug: only the title was passed to create_paper, so DOI/authors/year vanished and ingest then replaced the
    title with the PDF's font-heuristic guess."""
    from app.models import Paper
    from app.models.references import ExternalRef
    from app.models.workspace_search import WorkspaceSearchHit

    discovery_api.pdf_host.reply("/meta.pdf", 200, content=b"%PDF-1.4 meta content")
    hit_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/meta.pdf"])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    ref = await session.get(ExternalRef, hit.external_ref_id)
    ref.title, ref.authors, ref.year, ref.venue = "Imported Meta Title", ["Ada A", "Bo B"], 2025, "Sci Rep"
    ref.doi, ref.abstract = f"10.1038/import-meta-{hit_id}", "An abstract."
    await session.commit()

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.json() == {"imported": 1, "failed": 0}
    await session.refresh(hit)
    paper = await session.get(Paper, hit.paper_id)
    assert (paper.title, paper.authors, paper.year, paper.venue) == ("Imported Meta Title", ["Ada A", "Bo B"], 2025, "Sci Rep")
    assert (paper.doi, paper.abstract) == (ref.doi.lower(), "An abstract.")
    assert set(paper.manual_fields) == {"title", "doi"}


async def test_import_hits_whose_doi_is_already_in_the_library_reuses_that_paper(session, client, discovery_api, arq):
    from sqlalchemy import func, select

    from app.models import Paper
    from app.models.references import ExternalRef
    from app.models.workspace_search import WorkspaceSearchHit

    doi = "10.1038/already-here-%s" % uuid.uuid4().hex[:8]
    existing = Paper(title="Already In Library", file_path="/tmp/x.pdf", doi=doi)
    session.add(existing)
    await session.commit()
    discovery_api.pdf_host.reply("/dup.pdf", 200, content=b"%PDF-1.4 dup content")
    hit_id = await _make_importable_hit(session, pdf_urls=["https://pdf.example/dup.pdf"])
    hit = await session.get(WorkspaceSearchHit, hit_id)
    ref = await session.get(ExternalRef, hit.external_ref_id)
    ref.doi = doi.upper()
    await session.commit()

    resp = await client.post(f"/api/workspaces/{hit.workspace_id}/search/hits/import", json={"hit_ids": [str(hit_id)]})

    assert resp.json() == {"imported": 1, "failed": 0}
    await session.refresh(hit)
    await session.refresh(ref)
    assert hit.paper_id == existing.id and ref.imported_as == existing.id
    assert hit.acquisition_status == "imported"
    assert await session.scalar(select(func.count()).select_from(Paper).where(func.lower(Paper.doi) == doi.lower())) == 1
    assert arq.jobs == []
