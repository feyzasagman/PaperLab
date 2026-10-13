import uuid

import pytest
from sqlalchemy import delete, update

from app.models import LLMConnection, LLMModel
from tests.screening_pool import make_pool

pytestmark = pytest.mark.anyio


def _url(workspace, tail=""):
    return f"/api/workspaces/{workspace.id}/search/suggestions{tail}"


async def _with_criteria(session, rows=(("a", None),)):
    workspace, hits = await make_pool(session, list(rows))
    workspace.inclusion_criteria = "RCTs"
    await session.flush()
    return workspace, hits


async def test_start_enqueues_the_job_and_counts_what_is_left(client, session, arq):
    workspace, _ = await _with_criteria(session, [("a", None), ("b", None), ("c", "relevant")])
    resp = await client.post(_url(workspace), json={"confirm_remote": False})
    assert resp.status_code == 200
    assert (resp.json()["suggest_status"], resp.json()["suggest_total"]) == ("running", 2)
    assert arq.jobs == [("suggest_screening", str(workspace.id))]


async def test_start_without_criteria_is_409(client, session):
    workspace, _ = await make_pool(session, [("a", None)])
    assert (await client.post(_url(workspace), json={})).status_code == 409


async def test_start_while_running_is_409(client, session):
    workspace, _ = await _with_criteria(session)
    workspace.suggest_status = "running"
    await session.flush()
    assert (await client.post(_url(workspace), json={})).status_code == 409


async def test_start_without_a_default_model_is_409(client, session):
    await session.execute(delete(LLMModel))
    workspace, _ = await _with_criteria(session)
    resp = await client.post(_url(workspace), json={})
    assert resp.status_code == 409


async def test_a_cloud_model_needs_confirmation(client, session, arq):
    await session.execute(update(LLMConnection).values(kind="openai_compatible", base_url="https://api.example.com/v1"))
    workspace, _ = await _with_criteria(session)
    refused = await client.post(_url(workspace), json={"confirm_remote": False})
    assert refused.status_code == 409
    assert "api.example.com" in refused.json()["detail"]
    assert arq.jobs == []
    assert (await client.post(_url(workspace), json={"confirm_remote": True})).status_code == 200


async def test_stop_marks_a_running_job_stopping(client, session):
    workspace, _ = await _with_criteria(session)
    workspace.suggest_status = "running"
    await session.flush()
    assert (await client.post(_url(workspace, "/stop"))).json()["suggest_status"] == "stopping"


async def test_stop_when_idle_is_a_no_op(client, session):
    workspace, _ = await _with_criteria(session)
    assert (await client.post(_url(workspace, "/stop"))).json()["suggest_status"] == "idle"


async def test_stop_on_an_already_stopping_job_forces_it_idle(client, session):
    """A live incident on 2026-10-03 left a job stuck at "stopping" forever (its worker lost, no job behind it
    to ever finish the transition) — the only way back was a manual database fix. The advisory lock, not this
    status column, is what actually keeps two jobs from working a workspace at once, so a second Stop click is
    safe to treat as "force it idle" and gives the owner a self-service recovery path."""
    workspace, _ = await _with_criteria(session)
    workspace.suggest_status = "stopping"
    await session.flush()
    assert (await client.post(_url(workspace, "/stop"))).json()["suggest_status"] == "idle"


async def test_hits_carry_their_suggestion(client, session):
    workspace, [hit] = await _with_criteria(session)
    hit.suggestion, hit.suggestion_reason, hit.suggestion_note = "exclude", "language", "Not English."
    await session.flush()
    item = (await client.get(f"/api/workspaces/{workspace.id}/search/hits")).json()["items"][0]
    assert (item["suggestion"], item["suggestion_reason"], item["suggestion_note"]) == ("exclude", "language", "Not English.")


async def test_start_scoped_to_a_run_counts_only_that_run(client, session, arq):
    from app.models.workspace_search import WorkspaceSearchRun
    workspace, hits = await _with_criteria(session, [("a", None), ("b", None)])
    other = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q2", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=hits[0].first_seen_at, stats_json={},
    )
    session.add(other)
    await session.flush()
    hits[1].run_id = other.id
    await session.flush()
    resp = await client.post(_url(workspace), json={"run_id": str(other.id)})
    assert resp.status_code == 200
    assert (resp.json()["suggest_total"], resp.json()["suggest_run_id"]) == (1, str(other.id))


async def test_start_with_a_run_from_another_workspace_is_404(client, session, arq):
    workspace, _ = await _with_criteria(session)
    resp = await client.post(_url(workspace), json={"run_id": str(uuid.uuid4())})
    assert resp.status_code == 404
    assert arq.jobs == []
