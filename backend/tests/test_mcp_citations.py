"""M39 F7 MCP tool: export_citations."""

import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from pdf_papers import chunked_paper
from sqlalchemy import insert

from app.core import workspaces
from app.models import workspace_papers

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]


async def _workspace(session, tmp_path):
    paper = (await chunked_paper(session, tmp_path, title=f"Recall study {RUN}"))[0]
    paper.authors, paper.year = ["Ada Lovelace"], 2021
    ws = await workspaces.create(session, f"Cites {RUN}")
    await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=paper.id))


async def test_export_returns_text_in_the_asked_format_and_bibtex_by_default(session, tmp_path):
    await _workspace(session, tmp_path)
    default = result(await call("export_citations", workspace=f"Cites {RUN}"))
    assert default["format"] == "bibtex" and default["count"] == 1
    assert default["text"].startswith("@") and f"Recall study {RUN}" in default["text"]
    apa = result(await call("export_citations", workspace=f"Cites {RUN}", format="apa"))
    assert "Lovelace" in apa["text"] and "<i>" not in apa["text"]


async def test_unknown_format_and_unknown_workspace_are_recoverable_errors(session, tmp_path):
    await _workspace(session, tmp_path)
    bad = await call("export_citations", workspace=f"Cites {RUN}", format="docx")
    assert bad.is_error and bad.structured_content["error"] == "unknown_format"
    unknown = await call("export_citations", workspace="Nope " + RUN)
    assert unknown.structured_content["error"] == "unknown_workspace"
