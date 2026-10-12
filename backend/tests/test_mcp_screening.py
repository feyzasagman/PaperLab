"""M39 F1 MCP tools: search runs, hits, snowball, eligibility, PRISMA and notes."""

import dataclasses
import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result

from app.core import paper_sources, workspaces

pytestmark = pytest.mark.anyio
RUN = uuid.uuid4().hex[:8]


async def new_workspace(session, label: str):
    return await workspaces.create(session, f"{label} {RUN}")


async def test_start_search_run_enqueues_once_and_reports_its_run(session, jobs):
    await new_workspace(session, "Runs")
    run = result(
        await call("start_search_run", workspace=f"Runs {RUN}", query="llm creativity", sources=["arxiv", "crossref"])
    )

    # Stored in trust order (D73), not the order asked for.
    assert run["status"] == "running" and sorted(run["sources_json"]) == ["arxiv", "crossref"]
    assert jobs.jobs == [("run_workspace_search", run["id"])]
    assert result(await call("get_search_run", run_id=run["id"]))["id"] == run["id"]


async def test_start_search_run_defaults_to_the_sources_turned_on(session, jobs):
    await new_workspace(session, "Defaults")
    on = await paper_sources.get(session)
    run = result(await call("start_search_run", workspace=f"Defaults {RUN}", query="q"))
    assert run["sources_json"] and all(on.enabled[s] for s in run["sources_json"])


async def test_start_search_run_with_no_search_source_on_is_invalid(session, monkeypatch):
    await new_workspace(session, "None on")
    real = paper_sources.get

    async def all_off(s):
        sources = await real(s)
        return dataclasses.replace(sources, enabled=dict.fromkeys(sources.enabled, False))

    monkeypatch.setattr(paper_sources, "get", all_off)
    response = await call("start_search_run", workspace=f"None on {RUN}", query="q")
    assert response.is_error and response.structured_content["error"] == "invalid_input"


async def test_an_unknown_source_names_the_valid_ones(session):
    await new_workspace(session, "Bad source")
    response = await call("start_search_run", workspace=f"Bad source {RUN}", query="q", sources=["google_scholar"])
    assert response.is_error and response.structured_content["error"] == "unknown_source"
    assert "arxiv" in response.structured_content["valid"]


async def test_a_double_stop_is_a_readable_conflict(session):
    await new_workspace(session, "Twice")
    run = result(await call("start_search_run", workspace=f"Twice {RUN}", query="q", sources=["arxiv"]))
    stopped = result(await call("stop_search_run", run_id=run["id"]))
    stop_again = await call("stop_search_run", run_id=run["id"])

    assert stopped["status"] == "stopped"
    assert stop_again.is_error and stop_again.structured_content["error"] == "search_run_not_running"


async def test_an_unknown_run_is_not_found():
    response = await call("get_search_run", run_id=str(uuid.uuid4()))
    assert response.is_error and response.structured_content["error"] == "not_found"
