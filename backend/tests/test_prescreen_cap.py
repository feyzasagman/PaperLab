"""D265: auto pre-screening after a scheduled run takes the best AUTO_PRESCREEN_MAX new hits; manual Suggest is uncapped."""

import pytest
from screening_pool import make_pool
from sqlalchemy import select

from app.core import pipeline, queue, screening
from app.models.saved_search import SavedSearch
from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

ROWS = [(f"abstract {i} about sleep", None) for i in range(250)]


async def _scheduled_pool(session):
    workspace, _ = await make_pool(session, ROWS)
    workspace.inclusion_criteria = "RCTs"
    run = (await session.scalars(select(WorkspaceSearchRun).where(WorkspaceSearchRun.workspace_id == workspace.id))).one()
    saved = SavedSearch(workspace_id=workspace.id, query="q", schedule="0 6 * * *", timezone="UTC")
    session.add(saved)
    await session.flush()
    run.saved_search_id = saved.id
    await session.flush()
    return workspace, run


async def test_auto_prescreen_queues_only_the_cap_and_says_how_many_wait(session):
    workspace, run = await _scheduled_pool(session)
    assert await screening.prescreen_after_run(session, run) is None
    await session.refresh(workspace)
    assert (workspace.suggest_status, workspace.suggest_total) == ("running", 200)
    state = await screening.get_state(session, workspace.id)
    assert state["capped"] == "200 of 250" and state["capped_left"] == 50


async def test_manual_prescreen_is_not_capped(session, monkeypatch):
    workspace, run = await _scheduled_pool(session)

    async def no_enqueue(*args):
        pass

    monkeypatch.setattr(queue, "enqueue", no_enqueue)
    state = await pipeline.run_prescreen(session, workspace.name, run.id)
    assert state.suggest_total == 250 and state.capped is None


async def test_a_pool_under_the_cap_reports_nothing_left(session):
    workspace, run = await _scheduled_pool(session)
    for hit in (await session.scalars(select(WorkspaceSearchHit).where(WorkspaceSearchHit.run_id == run.id).offset(30))).all():
        await session.delete(hit)
    await session.flush()
    await screening.prescreen_after_run(session, run)
    state = await screening.get_state(session, workspace.id)
    assert state["suggest_total"] == 30 and state["capped"] is None
