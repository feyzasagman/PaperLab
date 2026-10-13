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

from app.core import graph, library, notes, pipeline, screening, workspaces
from app.core.errors import Conflict, DomainError, InvalidInput, NotFound
from app.db import SessionLocal

# ponytail: the embedding model loads on the first search (seconds). Warm it up in a lifespan if a first search
# times out in Claude Desktop.
mcp = MCPServer(
    "paperlab",
    instructions=(
        "PaperLab is the owner's research library. Learn workspace names with list_workspaces. "
        "Library: search_library, get_paper, related_papers, list_papers; create_workspace makes a new one; add papers "
        "by DOI, arXiv ID or PDF link with add_paper (free copies only), file them with add_paper_to_workspace and "
        "take them out with remove_paper_from_workspace. "
        "Systematic search: start_search_run, then get_search_run until it finishes "
        "(stop_search_run ends one early); read results with list_hits, "
        "record the owner's screening with review_hits, pull papers in with import_hit, widen with snowball. "
        "After full-text reading, record set_eligibility; get_prisma gives the flow counts. "
        "Saved searches: create_saved_search runs a search on a schedule, list_saved_searches shows them and "
        "set_saved_search_enabled pauses or resumes one. "
        "Notes: list_notes reads them, create_note saves one on a passage it quotes exactly. "
        "AI pre-screening: run_prescreen starts it, get_screening_suggestions reads the results and accept_suggestions "
        "accepts some. Claims: verify_claim and verify_claims check a claim against the papers it cites, with a "
        "quote as evidence. Evidence cards: generate_evidence_card starts one (about a minute per paper), "
        "get_evidence_card reads it, list_evidence_cards shows a workspace's cards. "
        "An AI suggestion on a hit is a suggestion, never a decision; only review_hits and "
        "accept_suggestions record one, so ask the owner first."
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
async def start_search_run(
    workspace: str, query: str, sources: list[str] | None = None,
    year_from: int | None = None, year_to: int | None = None,
) -> pipeline.SearchRunOut:
    """Use to search the paper sources for a topic and collect every result into the workspace's hits for screening.
    Runs in the background; poll get_search_run. `sources` defaults to the ones turned on in Settings. Give
    year_from/year_to to keep only papers published in that range (undated ones stay)."""
    async with sessions() as session:
        return await pipeline.start_search_run(session, workspace, query, sources, year_from, year_to)


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


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def get_screening_suggestions(
    workspace: str, run_id: uuid.UUID | None = None, min_relevance: int | None = None, cursor: str | None = None,
) -> pipeline.HitPage:
    """Use to read the AI's suggestions on undecided hits, best score first. min_relevance (1 to 5) hides weaker
    ones; pass next_cursor back to page. These are suggestions only; nothing here is a decision."""
    async with sessions() as session:
        return await pipeline.get_screening_suggestions(session, workspace, run_id, min_relevance, cursor)


@mcp.tool()
@recoverable
async def run_prescreen(workspace: str, run_id: uuid.UUID | None = None) -> pipeline.ScreeningStateOut:
    """Use to have the default model suggest include, exclude or unsure for each undecided hit (all hits, or one run).
    It runs in the background; a remote model is refused with remote_model_needs_confirm, since its provider would
    receive the abstracts. Suggestions are never decisions."""
    async with sessions() as session:
        return await pipeline.run_prescreen(session, workspace, run_id)


@mcp.tool()
@recoverable
async def accept_suggestions(workspace: str, hit_ids: list[uuid.UUID]) -> screening.AcceptResult:
    """Use to record the owner's decision on hits by accepting their suggestions (include becomes relevant,
    exclude becomes not_relevant with its reason). Hits with no suggestion, unsure ones, already decided ones
    and ones not in the workspace are skipped.
    Ask the owner before calling."""
    async with sessions() as session:
        return await pipeline.accept_suggestions(session, workspace, hit_ids)


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
    free copy was found; imported=0, failed=0 means it was already imported."""
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


@mcp.tool()
@recoverable
async def create_saved_search(
    workspace: str,
    query: str,
    schedule: str = "daily",
    time: str | None = "02:00",
    weekday: int | None = None,
    cron: str | None = None,
    sources: list[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    timezone: str = "UTC",
) -> pipeline.saved_searches.SavedSearchView:
    """Use to make a search run by itself on a schedule (overnight, weekly...): daily or weekly at HH:MM (weekday
    0 = Sunday), or a cron expression. Pass the owner's IANA time zone (e.g. Europe/Berlin); otherwise times are UTC.
    New hits land in the workspace for screening; PaperLab must be running at that time."""
    async with sessions() as session:
        return await pipeline.create_saved_search(
            session, workspace, query, schedule, time, weekday, cron, sources, year_from, year_to, timezone
        )


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def list_saved_searches(workspace: str) -> list[pipeline.saved_searches.SavedSearchView]:
    """Use to see a workspace's scheduled searches with their last and next run and any error."""
    async with sessions() as session:
        return await pipeline.list_saved_searches(session, workspace)


@mcp.tool()
@recoverable
async def set_saved_search_enabled(
    saved_search_id: uuid.UUID, enabled: bool
) -> pipeline.saved_searches.SavedSearchView:
    """Use to pause or resume a scheduled search."""
    async with sessions() as session:
        return await pipeline.set_saved_search_enabled(session, saved_search_id, enabled)


@mcp.tool()
@recoverable
async def verify_claim(
    claim: str,
    paper_id: uuid.UUID | None = None,
    doi: str | None = None,
    quoted_text: str | None = None,
    save: bool = False,
    allow_remote: bool = False,
) -> pipeline.claims.ClaimCheck:
    """Use to check whether a paper in the library supports a claim before you write it: give exactly one of
    paper_id or doi, and quoted_text if the claim quotes the paper. The verdict is an AI judgement backed by a quote
    from the paper; evidence_level tells how much text was available (full_text, abstract_only or metadata_only),
    and cannot_verify means there was no text to check, never a guess. confidence=low means the quote couldn't be
    found in the paper (or none was given): tell the owner the verdict is unconfirmed and show no quote as evidence.
    save=true keeps the check as an AI note on
    the paper. A remote default model is refused unless the owner agreed and you pass allow_remote=true."""
    async with sessions() as session:
        return await pipeline.verify_claim(session, claim, paper_id, doi, quoted_text, save, allow_remote)


@mcp.tool()
@recoverable
async def verify_claims(
    text: str, workspace: str | None = None, save: bool = False, allow_remote: bool = False
) -> list[pipeline.claims.ClaimItem]:
    """Use to check a pasted paragraph: every sentence with a citation (a DOI or author-year) is checked against
    the matching library paper, optionally only within one workspace. A citation it can't match is reported, never
    guessed; up to 30 claims per call. Verdicts are AI judgements backed by quotes; confidence=low means the quote
    couldn't be found in the paper (or none was given): tell the owner the verdict is unconfirmed and show no quote
    as evidence. save=true keeps each as an AI note. A remote default model is refused unless the owner agreed and
    you pass allow_remote=true."""
    async with sessions() as session:
        return await pipeline.verify_claims(session, text, workspace, save, allow_remote)


@mcp.tool()
@recoverable
async def generate_evidence_card(
    paper_id: uuid.UUID, regenerate: bool = False, confirm: bool = False, allow_remote: bool = False
) -> pipeline.evidence_cards.CardSummary:
    """Use to build an evidence card for a library paper: research question, design, sample, findings, effect sizes
    and the limitations its authors state. It starts the job and returns at once with status generating; generation
    takes about a minute per paper, so poll get_evidence_card until the status is ready or failed. The paper needs
    its full text. regenerate=true replaces a ready card; an edited card also needs confirm=true, so ask the owner
    first. A remote default model is refused unless the owner agreed and you pass allow_remote=true."""
    async with sessions() as session:
        return await pipeline.generate_evidence_card(session, paper_id, regenerate, confirm, allow_remote)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def get_evidence_card(paper_id: uuid.UUID) -> pipeline.evidence_cards.CardSummary:
    """Use to read a paper's evidence card, or to poll one that is generating (status generating, ready or failed).
    Every value is backed by a quote from the paper with its page, and items the paper's text could not back are left
    out: left_out counts them per field, so never fill them in. provenance llm_edited means the owner changed some
    values. Answers card_not_found when no card was started."""
    async with sessions() as session:
        return await pipeline.get_evidence_card(session, paper_id)


@mcp.tool(annotations=READ_ONLY)
@recoverable
async def list_evidence_cards(workspace: str) -> list[pipeline.EvidenceRow]:
    """Use to compare a workspace's papers: one row per paper (title, year, first author) with its evidence card, or
    no card yet. Every value is backed by a quote with its page, and unsupported items are left out (counted in
    left_out)."""
    async with sessions() as session:
        return await pipeline.list_evidence_cards(session, workspace)
