"""What Claude does over MCP (M39 F1): each function backs one tool, composing existing core services.

Workspaces come by name (D93). Jobs go through `queue`, paper sources through `discovery.open_providers`. No SQL here:
every read and write is an existing service's. Results are the API's own schemas, so Claude and the app see the same
fields.
"""

import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import discovery, paper_sources, papers, pdf_links, queue, workspace_search, workspaces
from app.core.errors import Conflict, InvalidInput, NotFound
from app.core.source_registry import DISCOVERY_SOURCE_IDS
from app.models import PaperStatus
from app.schemas.papers import PaperOut
from app.schemas.workspace_search import SearchRunOut

workspace_id = workspaces.by_name


@dataclass(frozen=True)
class Removed:
    workspace: str
    paper_id: uuid.UUID
    removed: bool = True


async def list_papers(session: AsyncSession, workspace: str) -> list[PaperOut]:
    rows = await workspaces.papers(session, await workspace_id(session, workspace))
    return [PaperOut.model_validate(row) for row in rows]


async def add_paper_to_workspace(session: AsyncSession, workspace: str, paper_id: uuid.UUID) -> PaperOut:
    await workspaces.add_paper(session, await workspace_id(session, workspace), paper_id)
    return PaperOut.model_validate(await papers.get_paper(session, paper_id))


async def remove_paper_from_workspace(session: AsyncSession, workspace: str, paper_id: uuid.UUID) -> Removed:
    await workspaces.remove_paper(session, await workspace_id(session, workspace), paper_id)
    return Removed(workspace=workspace.strip(), paper_id=paper_id)


@dataclass(frozen=True)
class AddPaperResult:
    status: Literal["queued", "ingested", "no_free_pdf"]
    paper_id: uuid.UUID | None = None
    landing_url: str | None = None
    title: str | None = None


def _identifier(identifier: str) -> tuple[str, str]:
    """("doi" | "arxiv" | "link", value). A title or an OpenAlex ID is refused (D217)."""
    text = identifier.strip().removeprefix("doi:").removeprefix("DOI:").strip()
    kind, value = discovery.classify_query(text)
    if kind in ("doi", "arxiv"):
        return kind, value
    if "://" in text:  # a title with a colon parses as a URL scheme, so only "://" marks a link
        return "link", text
    raise InvalidInput("invalid_identifier", hint="Use a DOI, an arXiv ID or a direct PDF link.")


def _landing_url(kind: str, value: str) -> str:
    return f"https://doi.org/{value}" if kind == "doi" else f"https://arxiv.org/abs/{value}"


async def _queued(session: AsyncSession, paper, workspace_uuid: uuid.UUID | None) -> AddPaperResult:
    if workspace_uuid is not None:
        await workspaces.add_paper(session, workspace_uuid, paper.id)
    await queue.enqueue("ingest_paper", str(paper.id))
    return AddPaperResult(status="queued", paper_id=paper.id, title=paper.title)


async def _add_found(session, providers, kind, value, workspace_uuid) -> AddPaperResult:
    found = await discovery.search(session, providers, value)
    match = next(
        (c for c in found.results if (c.doi or "").lower() == value or c.external_ids.get("arxiv") == value), None
    )
    if match is None:
        raise NotFound("no_matching_paper", identifier=value)
    if match.paper_id is not None:
        paper = await papers.get_paper(session, match.paper_id)
        if workspace_uuid is not None:
            await workspaces.add_paper(session, workspace_uuid, paper.id)
        status = "ingested" if paper.status == PaperStatus.READY else "queued"
        return AddPaperResult(status=status, paper_id=paper.id, title=paper.title)
    try:
        paper = await discovery.add(session, providers, match, settings.pdf_dir)
    except Conflict as exc:
        if str(exc) != discovery.NO_FREE_PDF:
            raise
        return AddPaperResult(status="no_free_pdf", landing_url=_landing_url(kind, value), title=match.title)
    return await _queued(session, paper, workspace_uuid)


async def _add_link(session, providers, link, workspace_uuid) -> AddPaperResult:
    await pdf_links.check_url(link)
    providers.pdf.event_hooks["request"].append(pdf_links.guard)  # this call's own client, closed after it
    data = await discovery.download_pdf(providers.pdf, [link])
    if data is None:
        return AddPaperResult(status="no_free_pdf", landing_url=link)
    # Like an upload: extraction and enrichment find the metadata. A paper already in the library becomes a second
    # copy, exactly as uploading the same PDF twice does (enrichment drops a DOI another paper holds).
    name = PurePosixPath(urlsplit(link).path).name or "paper.pdf"
    paper = await papers.create_paper(session, name, data, settings.pdf_dir)
    return await _queued(session, paper, workspace_uuid)


async def add_paper(session: AsyncSession, identifier: str, workspace: str | None = None) -> AddPaperResult:
    """Adds a paper by DOI, arXiv ID or direct PDF link, free copies only (D68, D217), and queues its ingest."""
    kind, value = _identifier(identifier)
    workspace_uuid = await workspace_id(session, workspace) if workspace else None  # before any download
    async with discovery.open_providers(session) as providers:
        if kind == "link":
            return await _add_link(session, providers, value, workspace_uuid)
        return await _add_found(session, providers, kind, value, workspace_uuid)


async def _sources(session: AsyncSession, sources: list[str] | None) -> list[str]:
    if sources:
        unknown = [s for s in sources if s not in DISCOVERY_SOURCE_IDS]
        if unknown:
            raise InvalidInput("unknown_source", unknown=unknown, valid=list(DISCOVERY_SOURCE_IDS))
        return sources
    on = (await paper_sources.get(session)).enabled
    chosen = [s for s in DISCOVERY_SOURCE_IDS if on.get(s)]
    if not chosen:
        raise InvalidInput("No paper source is on. Turn one on in Settings → Paper sources, or name `sources`.")
    return chosen


async def start_search_run(
    session: AsyncSession, workspace: str, query: str, sources: list[str] | None = None
) -> SearchRunOut:
    ws = await workspace_id(session, workspace)
    if not query.strip():
        raise InvalidInput("Give the search a query.")
    run = await workspace_search.start_run(session, ws, query.strip(), {}, await _sources(session, sources))
    await queue.enqueue("run_workspace_search", str(run.id))
    return SearchRunOut.model_validate(run)


async def get_search_run(session: AsyncSession, run_id: uuid.UUID) -> SearchRunOut:
    return SearchRunOut.model_validate(await workspace_search.get_run(session, run_id))


async def stop_search_run(session: AsyncSession, run_id: uuid.UUID) -> SearchRunOut:
    return SearchRunOut.model_validate(await workspace_search.stop_run(session, run_id))
