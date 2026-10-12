"""M39 F1 MCP tools: workspaces, papers and adding papers, through an in-process client against the test DB."""

import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from pdf_papers import chunked_paper

from app.core import papers, workspaces

pytestmark = pytest.mark.anyio
RUN = uuid.uuid4().hex[:8]  # the owner's workspaces share the dev database (D37)


async def test_create_then_list_workspaces_with_a_description():
    created = result(await call("create_workspace", name=f"Creativity {RUN}", description="LLMs and creativity"))
    listed = result(await call("list_workspaces"))

    assert created["name"] == f"Creativity {RUN}" and created["description"] == "LLMs and creativity"
    assert any(w["name"] == f"Creativity {RUN}" and w["paper_count"] == 0 for w in listed)


async def test_create_workspace_twice_is_a_readable_conflict():
    await call("create_workspace", name=f"Twice {RUN}")
    again = await call("create_workspace", name=f"Twice {RUN}")
    assert again.is_error and again.structured_content["error"] == "workspace_name_taken"


async def test_list_papers_shows_ingest_status(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Listed paper")
    ws = await workspaces.create(session, f"Papers {RUN}")
    await workspaces.add_paper(session, ws.id, paper.id)

    rows = result(await call("list_papers", workspace=f"Papers {RUN}"))

    assert [(r["id"], r["title"]) for r in rows] == [(str(paper.id), "Listed paper")]
    assert rows[0]["status"] == paper.status


async def test_list_papers_in_an_unknown_workspace_names_the_real_ones(session):
    await workspaces.create(session, f"Real {RUN}")
    response = await call("list_papers", workspace=f"Nope {RUN}")
    assert response.is_error and response.structured_content["error"] == "unknown_workspace"
    assert f"Real {RUN}" in response.structured_content["available"]


async def test_add_then_remove_a_paper_keeps_it_in_the_library(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Moved paper")
    ws = await workspaces.create(session, f"Move {RUN}")

    added = result(await call("add_paper_to_workspace", workspace=f"Move {RUN}", paper_id=str(paper.id)))
    removed = result(await call("remove_paper_from_workspace", workspace=f"Move {RUN}", paper_id=str(paper.id)))

    assert str(ws.id) in added["workspace_ids"]
    assert removed == {"workspace": f"Move {RUN}", "paper_id": str(paper.id), "removed": True}
    assert await workspaces.papers(session, ws.id) == []
    assert (await papers.get_paper(session, paper.id)).id == paper.id


async def test_add_an_unknown_paper_to_a_workspace_is_not_found(session):
    await workspaces.create(session, f"Ghost {RUN}")
    response = await call("add_paper_to_workspace", workspace=f"Ghost {RUN}", paper_id=str(uuid.uuid4()))
    assert response.is_error and response.structured_content["error"] == "not_found"
