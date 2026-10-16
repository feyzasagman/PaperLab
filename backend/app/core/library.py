"""The library as an MCP client reads it: passages for a question, and a paper's card. IDs and structure, never prose.

Each function returns plain dataclasses; the MCP server returns them as they are (M6, D93).
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import embedding_index, workspaces
from app.core import tags as tag_store
from app.core.errors import InvalidInput
from app.core.notes import list_notes_for_paper
from app.core.papers import get_paper
from app.core.retrieval import MAX_PER_PAPER, retrieve, searchable
from app.models import Chunk, Paper, Workspace, workspace_papers

SEARCH_K = 8


@dataclass(frozen=True)
class Passage:
    chunk_id: uuid.UUID
    paper_id: uuid.UUID
    paper_title: str
    year: int | None
    page: int
    section: str | None
    text: str
    distance: float
    source_type: (
        str | None
    )  # its paper's: journal_article, proceedings, book, chapter, preprint, magazine, thesis, other
    peer_reviewed: str  # true | false | unknown (a heuristic; the owner can set it)
    published_venue: str | None  # where a preprint was published, when known
    evidence_access: str  # full_text | abstract | metadata


@dataclass(frozen=True)
class Section:
    title: str
    page: int  # where the section starts


@dataclass(frozen=True)
class NoteBrief:
    id: uuid.UUID
    provenance: str  # human | llm | llm_edited: an AI note must never be quoted as the owner's own words (D90)
    body: str
    page: int | None  # its first passage on this paper; None for a note on the whole paper
    quoted_text: str | None


@dataclass(frozen=True)
class PaperCard:
    id: uuid.UUID
    title: str
    authors: list[str]
    year: int | None
    venue: str | None
    doi: str | None
    abstract: str | None
    status: str
    page_count: int | None
    is_retracted: bool
    source_type: str | None
    peer_reviewed: str
    evidence_access: str
    published_doi: str | None  # a preprint's published version, when known
    published_venue: str | None
    reading_pass: int
    triage: str | None
    workspaces: list[str]  # names, alphabetical
    sections: list[Section]  # in reading order
    notes: list[NoteBrief]  # notes on the whole paper first, newest first; then in reading order


async def search(
    session: AsyncSession, query: str, workspace: str | None = None, *, k: int = SEARCH_K, embedder=None,
    tags: list[str] | None = None, match: Literal["any", "all"] = "any",
) -> list[Passage]:
    """The k passages nearest to the query, closest first, at most MAX_PER_PAPER from one paper, in the whole library
    or one workspace (by name). The name is checked before anything is embedded, so a wrong one never loads the model.

    `tags` (a workspace's tag names) keeps only papers carrying any/all of them. Raises
    InvalidInput("empty_query" | "tags_need_a_workspace" | "unknown_tag" | "invalid_match"),
    NotFound("unknown_workspace", available=[...]),
    Conflict("search_not_set_up" | "search_rebuilding", detail=<sentence>, …), Conflict("embedding_model_changed"), or
    Conflict(<the question can't be embedded>).
    """
    if not query.strip():
        raise InvalidInput("empty_query")
    if tags and workspace is None:
        raise InvalidInput("tags_need_a_workspace")
    wanted = None
    if workspace is None:
        workspace_id, scope = None, await embedding_index.indexed_papers(session)
    else:
        workspace_id = await workspaces.by_name(session, workspace)
        wanted = await tag_store.paper_ids(session, workspace_id, tags, match)
        scope = [paper.id for paper in await workspaces.papers(session, workspace_id)]
        if wanted is not None:  # retrieve takes paper_ids or workspace_id, never both
            scope, workspace_id = [p for p in scope if p in wanted], None
    if wanted is not None and not scope:
        return []
    embedder = await searchable(session, scope, embedder)  # not set up, then rebuilding, then stale vectors (D156)
    found = await retrieve(
        session, query, workspace_id=workspace_id, paper_ids=scope if wanted is not None else None,
        k=k, embedder=embedder, per_paper=MAX_PER_PAPER,
    )
    rows = await session.scalars(
        select(Paper).where(Paper.id.in_({chunk.paper_id for chunk in found})).execution_options(populate_existing=True)
    )
    papers = {paper.id: paper for paper in rows}
    return [
        Passage(
            chunk_id=chunk.id,
            paper_id=chunk.paper_id,
            paper_title=papers[chunk.paper_id].title,
            year=papers[chunk.paper_id].year,
            page=chunk.page,
            section=chunk.section,
            text=chunk.text,
            distance=round(chunk.distance, 4),
            source_type=papers[chunk.paper_id].source_type,
            peer_reviewed=papers[chunk.paper_id].peer_reviewed,
            published_venue=papers[chunk.paper_id].published_venue,
            evidence_access=papers[chunk.paper_id].evidence_access,
        )
        for chunk in found
    ]


async def paper_card(session: AsyncSession, paper_id: uuid.UUID) -> PaperCard:
    """A paper's metadata, its section outline, its workspaces and every note on it with its provenance. No chunk text:
    search gives passages. Raises NotFound."""
    paper = await get_paper(session, paper_id)
    await session.refresh(paper)  # evidence_access reads chunks, which a bulk write may have changed behind this object
    names = select(Workspace.name).join(workspace_papers).where(workspace_papers.c.paper_id == paper_id)
    starts = await session.execute(
        select(Chunk.section_title, Chunk.page).where(Chunk.paper_id == paper_id).order_by(Chunk.ordinal)
    )
    sections: dict[str, Section] = {}
    for title, page in starts:
        if title and title not in sections:
            sections[title] = Section(title=title, page=page)
    notes = []
    for note in await list_notes_for_paper(session, paper_id):
        here = [anchor for anchor in note.anchors if anchor.paper_id == paper_id]
        first = min(here, key=lambda anchor: (anchor.page, min(rect[1] for rect in anchor.bbox)), default=None)
        page, quote = (None, None) if first is None else (first.page, first.quoted_text)
        notes.append(NoteBrief(note.id, note.provenance, note.body, page, quote))
    return PaperCard(
        id=paper.id,
        title=paper.title,
        authors=paper.authors,
        year=paper.year,
        venue=paper.venue,
        doi=paper.doi,
        abstract=paper.abstract,
        status=paper.status,
        page_count=paper.page_count,
        is_retracted=paper.is_retracted,
        source_type=paper.source_type,
        peer_reviewed=paper.peer_reviewed,
        evidence_access=paper.evidence_access,
        published_doi=paper.published_doi,
        published_venue=paper.published_venue,
        reading_pass=paper.reading_pass,
        triage=paper.triage,
        workspaces=sorted(await session.scalars(names)),
        sections=list(sections.values()),
        notes=notes,
    )
