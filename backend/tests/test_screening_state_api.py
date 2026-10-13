import pytest
from sqlalchemy import delete

from app.core import screening
from app.core.errors import Conflict
from app.models import LLMModel
from app.models.workspace_search import WorkspaceSearchRun
from tests.screening_pool import BASE, make_pool

pytestmark = pytest.mark.anyio


def _url(workspace):
    return f"/api/workspaces/{workspace.id}/search/screening"


async def test_state_defaults_and_names_the_default_model(client, session):
    workspace, _ = await make_pool(session, [])
    body = (await client.get(_url(workspace))).json()
    assert body["criteria"] is None
    assert (body["suggest_status"], body["suggest_done"], body["suggest_total"]) == ("idle", 0, 0)
    assert set(body) >= {"model_label", "model_is_local", "model_host"}


async def test_state_without_a_default_model(client, session):
    await session.execute(delete(LLMModel))
    workspace, _ = await make_pool(session, [])
    body = (await client.get(_url(workspace))).json()
    assert (body["model_label"], body["model_is_local"], body["model_host"]) == (None, None, None)


async def test_saving_criteria_clears_stored_suggestions(client, session):
    workspace, [hit] = await make_pool(session, [("sleep", None)])
    hit.suggestion, hit.suggestion_model = "include", "Ollama · qwen3:8b"
    await session.flush()
    resp = await client.put(_url(workspace), json={"criteria": "RCTs on sleep in adults"})
    assert resp.json()["criteria"] == "RCTs on sleep in adults"
    await session.refresh(hit)
    assert (hit.suggestion, hit.suggestion_model) == (None, None)


async def test_saving_the_same_criteria_keeps_suggestions(client, session):
    workspace, [hit] = await make_pool(session, [("sleep", None)])
    workspace.inclusion_criteria = "RCTs"
    hit.suggestion = "include"
    await session.flush()
    await client.put(_url(workspace), json={"criteria": "RCTs"})
    await session.refresh(hit)
    assert hit.suggestion == "include"


async def test_criteria_over_the_limit_is_422(client, session):
    workspace, _ = await make_pool(session, [])
    assert (await client.put(_url(workspace), json={"criteria": "x" * 4001})).status_code == 422


async def test_blank_criteria_are_stored_as_none(client, session):
    workspace, _ = await make_pool(session, [])
    assert (await client.put(_url(workspace), json={"criteria": "   "})).json()["criteria"] is None


async def test_criteria_cannot_change_while_suggesting(client, session):
    workspace, [hit] = await make_pool(session, [("sleep", None)])
    workspace.suggest_status = "running"
    hit.suggestion = "include"
    await session.flush()
    resp = await client.put(_url(workspace), json={"criteria": "new"})
    assert resp.status_code == 409
    await session.refresh(hit)
    assert hit.suggestion == "include"


async def test_the_three_criteria_save_and_any_change_clears_every_suggestion_column(client, session):
    workspace, [hit] = await make_pool(session, [("sleep", None)])
    hit.suggestion, hit.suggestion_relevance, hit.suggestion_quotes = "include", 4, ["q"]
    hit.suggestion_confidence, hit.suggestion_flags, hit.suggestion_prompt_version = "high", [], 2
    await session.flush()
    body = (await client.put(_url(workspace), json={"research_question": "Does sleep help?"})).json()
    assert body["research_question"] == "Does sleep help?" and body["criteria"] is None
    await session.refresh(hit)
    assert (hit.suggestion, hit.suggestion_relevance, hit.suggestion_quotes, hit.suggestion_confidence,
            hit.suggestion_flags, hit.suggestion_prompt_version) == (None,) * 6


async def test_the_old_criteria_field_maps_to_inclusion(client, session):
    workspace, _ = await make_pool(session, [])
    body = (await client.put(_url(workspace), json={"criteria": "RCTs"})).json()
    assert (body["criteria"], body["inclusion_criteria"], body["exclusion_criteria"]) == ("RCTs", "RCTs", None)


async def test_start_needs_a_question_or_inclusion_and_scopes_the_queue(session):
    workspace, hits = await make_pool(session, [("a", None), ("b", None)])
    with pytest.raises(Conflict):
        await screening.start_suggestions(session, workspace.id, True)
    workspace.research_question = "Does sleep help?"
    other = WorkspaceSearchRun(
        workspace_id=workspace.id, query_text="q2", filters_json={}, query_overrides_json={},
        sources_json=["arxiv"], status="exhausted", started_at=BASE, stats_json={},
    )
    session.add(other)
    await session.flush()
    hits[1].run_id = other.id
    await session.flush()
    state = await screening.start_suggestions(session, workspace.id, True, run_id=other.id)
    assert (state["suggest_total"], state["suggest_run_id"]) == (1, other.id)


async def test_prescreen_settings_save_and_validate(session):
    workspace, _ = await make_pool(session, [])
    state = await screening.set_prescreen_settings(
        session, workspace.id, after_scheduled_runs=False, auto_import_min_relevance=4)
    assert (state["prescreen_after_scheduled_runs"], state["auto_import_min_relevance"]) == (False, 4)
    with pytest.raises(Conflict):
        await screening.set_prescreen_settings(session, workspace.id, after_scheduled_runs=None,
                                               auto_import_min_relevance=3)


async def test_put_saves_the_two_prescreen_settings_in_any_subset(client, session):
    workspace, _ = await make_pool(session, [])
    first = (await client.put(_url(workspace), json={"auto_import_min_relevance": 4})).json()
    assert (first["auto_import_min_relevance"], first["prescreen_after_scheduled_runs"]) == (4, True)
    second = (await client.put(_url(workspace), json={"prescreen_after_scheduled_runs": False})).json()
    assert (second["auto_import_min_relevance"], second["prescreen_after_scheduled_runs"]) == (4, False)
    third = (await client.put(_url(workspace), json={"auto_import_min_relevance": None})).json()
    assert (third["auto_import_min_relevance"], third["prescreen_after_scheduled_runs"]) == (None, False)


async def test_put_criteria_and_settings_together(client, session):
    workspace, _ = await make_pool(session, [])
    body = (await client.put(_url(workspace), json={
        "research_question": "Does sleep help?", "exclusion_criteria": "animals", "auto_import_min_relevance": 5,
    })).json()
    assert (body["research_question"], body["exclusion_criteria"], body["auto_import_min_relevance"]) == (
        "Does sleep help?", "animals", 5)


@pytest.mark.parametrize("value", [3, 6, 0])
async def test_put_rejects_an_auto_import_bar_outside_4_and_5(client, session, value):
    workspace, _ = await make_pool(session, [])
    assert (await client.put(_url(workspace), json={"auto_import_min_relevance": value})).status_code == 422


async def test_put_criteria_alone_still_means_inclusion(client, session):
    workspace, _ = await make_pool(session, [])
    body = (await client.put(_url(workspace), json={"criteria": "RCTs"})).json()
    assert (body["criteria"], body["inclusion_criteria"], body["auto_import_min_relevance"]) == ("RCTs", "RCTs", None)
