"""PaperLab over MCP, on stdio (M6). Claude Desktop starts it inside the api container (D89):

    docker compose -f <repo>/docker-compose.yml exec -T api python -m mcp_server

Each tool opens a session, calls one core service and returns its dataclasses. There is no SQL and no logic here: a
DomainError becomes a result the client's model can read and retry with (D93). Stdout is the protocol pipe, so
nothing in this process may print.
"""

import functools
import json
import re
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from app.core import graph, library, notes, pipeline, workspaces
from app.core.errors import Conflict, DomainError, InvalidInput, NotFound
from app.db import SessionLocal

# ponytail: the embedding model loads on the first search (seconds). Warm it up in a lifespan if a first search
# times out in Claude Desktop.
mcp = MCPServer(
    "paperlab",
    instructions=(
        "PaperLab is the owner's research library. Search it with search_library, read a paper's outline and notes "
        "with get_paper, walk to connected papers with related_papers, and save a note on a passage with create_note."
    ),
)
sessions = SessionLocal  # tests point this at their rolled-back session
READ_ONLY = ToolAnnotations(read_only_hint=True)
_CODE = re.compile(r"[a-z]+(?:_[a-z]+)+")
_KINDS = {NotFound: "not_found", InvalidInput: "invalid_input", Conflict: "conflict"}


def recoverable(tool):
    """Turns a DomainError into an is_error result: {"error": code, **details}. The code is the error's message when
    that is one (e.g. unknown_workspace), else its kind, with the message as "detail"."""

    @functools.wraps(tool)
    async def call(*args, **kwargs):
        try:
            return await tool(*args, **kwargs)
        except DomainError as exc:
            message = str(exc)
            if _CODE.fullmatch(message):
                error = {"error": message, **exc.details}
            else:
                error = {"error": _KINDS.get(type(exc), "invalid_input"), "detail": message, **exc.details}
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(error))], structured_content=error, is_error=True
            )

    return call


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def search_library(query: str, workspace: str | None = None) -> list[library.Passage]:
    """Passages from the owner's papers closest to `query`, closest first (at most 3 from one paper). Leave out
    `workspace` to search the whole library, or give a workspace's exact name; an unknown name answers with the
    names that exist. Until the owner downloads the search model, it answers search_not_set_up with a sentence to
    pass on. While search is being rebuilt with a new source, it answers search_rebuilding with how far it got (done
    of total papers): try again when it finishes."""
    async with sessions() as session:
        return await library.search(session, query, workspace)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def get_paper(paper_id: uuid.UUID) -> library.PaperCard:
    """A paper's details, its section outline with start pages, its workspaces, and every note on it. reading_pass is
    how many of Keshav's three passes the owner has finished (0–3), and triage their keep / later / drop decision, or
    null. Only the owner sets them. Each note has a provenance: `human` is the owner's own writing; `llm` and
    `llm_edited` were written by an AI."""
    async with sessions() as session:
        return await library.paper_card(session, paper_id)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def related_papers(paper_id: uuid.UUID, hops: int = 1) -> list[graph.Related]:
    """Library papers connected to this one within `hops` links (1 to 3), nearest first. `via` says how: same_workspace,
    co_anchored (a note on both), co_authored, shares_topic, cites (this paper cites it), cited_by (it cites this
    paper), similar (one is among the other's three closest by content) or manual (the owner linked them by hand).
    `similar` counts for the first hop only; further hops follow the links someone made. While search is being
    rebuilt with a new source, similar links only the papers already embedded again."""
    async with sessions() as session:
        return await graph.related(session, paper_id, hops)


@mcp.tool()
@recoverable
async def create_note(body: str, paper_id: uuid.UUID, quoted_text: str) -> notes.NoteView:
    """Save a note on a paper, anchored on `quoted_text`. Copy quoted_text exactly from one passage search_library
    returned for this paper, long enough to occur only once. The note is shown to the owner as written by AI."""
    async with sessions() as session:
        return await notes.create_llm_note(session, paper_id, body, quoted_text)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def list_workspaces() -> list[workspaces.WorkspaceView]:
    """Use first, to learn the exact workspace names every other tool takes. Each has its paper and note counts."""
    async with sessions() as session:
        return await workspaces.list_workspaces(session)


@mcp.tool()
@recoverable
async def create_workspace(name: str, description: str | None = None) -> workspaces.WorkspaceView:
    """Use when starting a new research topic: makes an empty workspace (name up to 80 characters)."""
    async with sessions() as session:
        return await workspaces.create(session, name, description)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def list_papers(workspace: str) -> list[pipeline.PaperOut]:
    """Use to see which papers a workspace holds and whether each finished ingesting (status `ready`), newest first."""
    async with sessions() as session:
        return await pipeline.list_papers(session, workspace)


@mcp.tool()
@recoverable
async def add_paper_to_workspace(workspace: str, paper_id: uuid.UUID) -> pipeline.PaperOut:
    """Use to file a paper already in the library into a workspace. Adding it twice is harmless."""
    async with sessions() as session:
        return await pipeline.add_paper_to_workspace(session, workspace, paper_id)


@mcp.tool()
@recoverable
async def remove_paper_from_workspace(workspace: str, paper_id: uuid.UUID) -> pipeline.Removed:
    """Use to take a paper out of a workspace. The paper and its notes stay in the library."""
    async with sessions() as session:
        return await pipeline.remove_paper_from_workspace(session, workspace, paper_id)


@mcp.tool()
@recoverable
async def add_paper(identifier: str, workspace: str | None = None) -> pipeline.AddPaperResult:
    """Use to bring one paper into the library by DOI, arXiv ID or a direct PDF link (free copies only). Returns
    `queued` (ingesting now), `ingested` (already in the library and ready) or `no_free_pdf` with its landing page.
    Give `workspace` to file it there too."""
    async with sessions() as session:
        return await pipeline.add_paper(session, identifier, workspace)


@mcp.tool()
@recoverable
async def start_search_run(workspace: str, query: str, sources: list[str] | None = None) -> pipeline.SearchRunOut:
    """Use to search the paper sources for a topic and collect every result into the workspace's hits for screening.
    Runs in the background; poll get_search_run. `sources` defaults to the ones turned on in Settings."""
    async with sessions() as session:
        return await pipeline.start_search_run(session, workspace, query, sources)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def get_search_run(run_id: uuid.UUID) -> pipeline.SearchRunOut:
    """Use to check a search run's progress: status (running, exhausted, stopped) and per-source counts in stats."""
    async with sessions() as session:
        return await pipeline.get_search_run(session, run_id)


@mcp.tool()
@recoverable
async def stop_search_run(run_id: uuid.UUID) -> pipeline.SearchRunOut:
    """Use to stop a running search early. Hits found so far stay."""
    async with sessions() as session:
        return await pipeline.stop_search_run(session, run_id)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def list_hits(
    workspace: str, run_id: uuid.UUID | None = None, status: str | None = None, cursor: str | None = None,
    limit: int = 50,
) -> pipeline.HitPage:
    """Use to read search results waiting for screening. Filter by run and by status (undecided, relevant,
    not_relevant, maybe); pass next_cursor back to page. Each hit may carry an AI suggestion — a suggestion only,
    never a decision."""
    async with sessions() as session:
        return await pipeline.list_hits(session, workspace, run_id, status, cursor, limit)


@mcp.tool()
@recoverable
async def review_hits(
    workspace: str, hit_ids: list[uuid.UUID], decision: str, reason: str | None = None
) -> pipeline.ReviewResult:
    """Use to record the owner's title/abstract screening decision on hits: relevant, not_relevant (needs a reason:
    wrong_topic, wrong_study_type, duplicate, language, inaccessible, other) or maybe."""
    async with sessions() as session:
        return await pipeline.review_hits(session, workspace, hit_ids, decision, reason)


@mcp.tool()
@recoverable
async def import_hit(workspace: str, hit_id: uuid.UUID) -> pipeline.workspace_search.ImportResult:
    """Use to pull a screened hit into the library: downloads its free PDF and starts ingesting. failed=1 means no
    free copy was found."""
    async with sessions() as session:
        return await pipeline.import_hit(session, workspace, hit_id)


@mcp.tool()
@recoverable
async def snowball(
    workspace: str, seed_paper_ids: list[uuid.UUID], direction: str
) -> pipeline.workspace_search.SnowballResult:
    """Use to find papers a workspace's seed papers cite (backward), are cited by (forward) or both, one hop via
    Semantic Scholar. New ones land in the hits for screening."""
    async with sessions() as session:
        return await pipeline.snowball(session, workspace, seed_paper_ids, direction)


@mcp.tool()
@recoverable
async def set_eligibility(
    workspace: str, paper_id: uuid.UUID, run_id: uuid.UUID, verdict: str, reason: str | None = None
) -> pipeline.EligibilityView:
    """Use after reading an imported paper's full text to record the owner's stage-2 verdict for a run: include or
    exclude (give the reason)."""
    async with sessions() as session:
        return await pipeline.set_eligibility(session, workspace, paper_id, run_id, verdict, reason)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def get_prisma(workspace: str, run_id: uuid.UUID | None = None) -> pipeline.prisma_export.PrismaExport:
    """Use to get the PRISMA flow counts for one run, or for the whole workspace when run_id is left out. Counts
    only the owner's decisions."""
    async with sessions() as session:
        return await pipeline.get_prisma(session, workspace, run_id)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def list_notes(
    workspace: str | None = None, paper_id: uuid.UUID | None = None, author: str = "all"
) -> list[pipeline.LabelledNote]:
    """Use to read what has been noted on a workspace's papers or on one paper: give exactly one of workspace or
    paper_id. author: you (the owner's), ai (written by a model, including ones the owner edited, labelled AI · edited)
    or all."""
    async with sessions() as session:
        return await pipeline.list_notes(session, workspace, paper_id, author)
