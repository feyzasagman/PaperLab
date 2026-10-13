"""The MCP tools through an in-process client, against the real database in the test's transaction (roadmap M6)."""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client
from pdf_papers import TWO_LINE_QUOTE, chunked_paper
from sqlalchemy import delete
from test_search_rebuild import OLLAMA_NAME, paper_with, switched

from app.core import papers, workspaces
from app.models import Chunk, LLMOutput, Note, Paper
from app.providers import embedding
from mcp_server import server

pytestmark = pytest.mark.anyio

RUN = uuid.uuid4().hex[:8]  # the owner's workspaces share the dev database (D37)


@pytest.fixture(autouse=True)
def tools_use_the_test_session(session, embedder, monkeypatch):
    """Each tool opens `server.sessions()`: give it this test's rolled-back session, and a fake query embedder."""

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(server, "sessions", test_session)
    monkeypatch.setattr(embedding, "get_model", lambda: embedder)


async def call(tool: str, **arguments):
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments)


EXPECTED_TOOLS = {
    "search_library": True, "get_paper": True, "related_papers": True, "create_note": None,
    "list_workspaces": True, "create_workspace": None, "list_papers": True, "add_paper": None,
    "add_paper_to_workspace": None, "remove_paper_from_workspace": None,
    "start_search_run": None, "get_search_run": True, "stop_search_run": None,
    "list_hits": True, "review_hits": None, "import_hit": None, "snowball": None,
    "set_eligibility": None, "get_prisma": True, "list_notes": True,
    "create_saved_search": None, "list_saved_searches": True, "set_saved_search_enabled": None,
    "run_prescreen": None, "get_screening_suggestions": True, "accept_suggestions": None,
}


async def test_every_tool_and_which_of_them_only_read():
    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    hints = {name: tool.annotations and tool.annotations.read_only_hint for name, tool in tools.items()}
    assert hints == EXPECTED_TOOLS
    assert all(tool.output_schema for tool in tools.values())
    assert all(tool.description and tool.description.startswith("Use ") for name, tool in tools.items()
               if name not in ("search_library", "get_paper", "related_papers", "create_note"))


async def test_the_server_holds_no_sql_and_imports_only_core():
    source = Path(server.__file__).read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert all(
        line.startswith(("from app.core", "import functools", "import json", "import re", "import uuid", "from mcp"))
        or line == "from app.db import SessionLocal"
        for line in imports
    ), imports
    assert "select(" not in source and "execute(" not in source


async def test_search_library_returns_ids_and_structure_not_prose(session, tmp_path, embedder):
    paper, chunks, vectors = await chunked_paper(session, tmp_path, title="Searched paper")
    workspace = await workspaces.create(session, f"MCP search {RUN}")
    await workspaces.add_paper(session, workspace.id, paper.id)
    embedder.vectors["search_query: how are notes anchored?"] = vectors[0]

    result = await call("search_library", query="how are notes anchored?", workspace=f"MCP search {RUN}")

    assert not result.is_error, result.content
    passages = result.structured_content["result"]
    assert [p["chunk_id"] for p in passages] == [str(chunks[0].id), str(chunks[1].id)]
    assert passages[0] == {
        "chunk_id": str(chunks[0].id),
        "paper_id": str(paper.id),
        "paper_title": "Searched paper",
        "year": None,
        "page": 1,
        "section": "1 Introduction",
        "text": chunks[0].text,
        "distance": 0.0,
    }


async def test_with_no_search_model_search_answers_a_recoverable_error_with_a_sentence(monkeypatch):
    monkeypatch.setattr(embedding, "get_model", lambda: None)  # nothing downloaded

    result = await call("search_library", query="how are notes anchored?")

    assert result.is_error
    assert result.structured_content == {
        "error": "search_not_set_up",
        "detail": "Search isn't set up. Download the search model in Settings to search long papers and workspaces.",
    }


async def test_mid_rebuild_search_answers_a_recoverable_error_with_how_far_it_got(session):
    await session.execute(delete(Chunk))  # D15: the rebuild counts only this test's papers
    await paper_with(session, OLLAMA_NAME)
    await paper_with(session, "test")
    source = await switched(session)

    result = await call("search_library", query="how are notes anchored?")

    assert result.is_error
    assert result.structured_content == {
        "error": "search_rebuilding",
        "detail": f"Search is being rebuilt with {source.label}: 1 of 2 papers. Try again when it finishes.",
        "done": 1,
        "total": 2,
    }


async def test_an_unknown_workspace_answers_with_the_real_names(session):
    await workspaces.create(session, f"MCP thesis {RUN}")

    result = await call("search_library", query="anything", workspace="No such workspace")

    # Not compared with a later read: a workspace another run commits in between (E2E on this stack) would differ.
    available = result.structured_content.get("available", [])
    assert result.is_error
    assert result.structured_content == {"error": "unknown_workspace", "available": available}
    assert f"MCP thesis {RUN}" in available  # the database's collation, not Python's sort, orders the names


async def test_create_note_places_the_quote_and_stores_an_llm_note(session, tmp_path):
    paper, chunks, _ = await chunked_paper(session, tmp_path)

    result = await call(
        "create_note", body="Claims can be checked.", paper_id=str(paper.id), quoted_text=TWO_LINE_QUOTE
    )

    assert not result.is_error, result.content
    note = result.structured_content
    [anchor] = note["anchors"]
    assert (note["provenance"], anchor["page"], anchor["quoted_text"]) == ("llm", 1, TWO_LINE_QUOTE)
    assert len(anchor["bbox"]) == 2 and anchor["bbox"][0] != chunks[0].bbox[1]  # the quoted lines, not the paragraph
    stored = await session.get(Note, uuid.UUID(note["id"]))
    output = await session.get(LLMOutput, stored.source_id)
    assert (stored.provenance, output.kind, output.content) == ("llm", "mcp", "Claims can be checked.")


async def test_a_quote_that_is_not_in_the_paper_is_a_recoverable_error(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path)

    result = await call("create_note", body="n", paper_id=str(paper.id), quoted_text="Words this paper never uses.")

    assert result.is_error
    assert result.structured_content["error"] == "quote_not_found"
    assert result.structured_content["hint"]
    assert '"quote_not_found"' in result.content[0].text  # clients that only read text see the same error


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_paper", {}),
        ("related_papers", {"hops": 2}),
        ("create_note", {"body": "b", "quoted_text": "q"}),
    ],
)
async def test_unknown_paper_ids_are_recoverable_errors(tool, arguments):
    unknown = uuid.uuid4()

    result = await call(tool, paper_id=str(unknown), **arguments)

    assert result.is_error
    assert result.structured_content == {"error": "not_found", "detail": f"paper {unknown} not found"}


async def test_a_malformed_paper_id_is_refused_by_argument_validation():
    result = await call("get_paper", paper_id="not-a-uuid")

    assert result.is_error and "paper_id" in result.content[0].text


async def test_get_paper_sends_every_note_with_its_provenance(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Carded paper")
    await call("create_note", body="Claims can be checked.", paper_id=str(paper.id), quoted_text=TWO_LINE_QUOTE)

    card = (await call("get_paper", paper_id=str(paper.id))).structured_content

    assert (card["title"], [s["title"] for s in card["sections"]]) == ("Carded paper", ["1 Introduction", "2 Method"])
    assert [(n["provenance"], n["body"], n["page"]) for n in card["notes"]] == [("llm", "Claims can be checked.", 1)]


async def test_get_paper_shows_reading_pass_and_triage(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, title="Carded paper")
    await papers.set_reading(session, paper.id, {"reading_pass": 3, "triage": "drop"})

    card = (await call("get_paper", paper_id=str(paper.id))).structured_content

    assert (card["reading_pass"], card["triage"]) == (3, "drop")


async def test_related_papers_respects_hops(session):
    # Bare papers, no chunks: since M7 `related()` shares the graph's edge set, where `similar` is a library-wide
    # top-3 over embeddings. A chunked paper here would also link to whatever else the test database holds, and
    # `chunked_paper`'s vectors are random per call (D37), so the walk would differ from run to run.
    first, second, third = (Paper(title=title, file_path="/nonexistent.pdf") for title in ("First", "Second", "Third"))
    session.add_all([first, second, third])
    await session.commit()
    for name, pair in [(f"MCP ab {RUN}", (first, second)), (f"MCP bc {RUN}", (second, third))]:
        workspace = await workspaces.create(session, name)
        for paper in pair:
            await workspaces.add_paper(session, workspace.id, paper.id)

    one = await call("related_papers", paper_id=str(first.id), hops=1)
    two = await call("related_papers", paper_id=str(first.id), hops=2)
    too_far = await call("related_papers", paper_id=str(first.id), hops=4)

    assert [(r["title"], r["hops"], r["via"]) for r in one.structured_content["result"]] == [
        ("Second", 1, ["same_workspace"])
    ]
    assert [(r["title"], r["hops"]) for r in two.structured_content["result"]] == [("Second", 1), ("Third", 2)]
    assert too_far.is_error and too_far.structured_content == {"error": "hops_out_of_range", "allowed": [1, 3]}
