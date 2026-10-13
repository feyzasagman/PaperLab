from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import DiscoveryDep, SessionDep
from app.config import settings
from app.core import discovery, workspaces
from app.core.errors import InvalidInput
from app.core.queue import INGEST_QUEUE
from app.schemas.discovery import AddPaperRequest, SearchOut
from app.schemas.papers import PaperOut

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/search")
async def search_papers(
    q: Annotated[str, Query(max_length=500)], session: SessionDep, providers: DiscoveryDep
) -> SearchOut:
    """Papers for a title, DOI, arXiv ID or OpenAlex ID from every paper source that is on, merged, each marked when
    the library already holds it; `notices` name the sources that failed. 409 when no source that is on can look the
    query up, or when every one that was asked failed."""
    if not q.strip():
        raise InvalidInput("Type a title, DOI, arXiv ID or OpenAlex ID.")
    return await discovery.search(session, providers, q)


@router.post("/add", status_code=201)
async def add_paper(
    payload: AddPaperRequest, request: Request, session: SessionDep, providers: DiscoveryDep
) -> PaperOut:
    """Downloads the paper's first free PDF and ingests it like an upload. 404 for an unknown workspace (before any
    download); 409 when the paper is already in the library or no free PDF is found."""
    if payload.workspace_id is not None:
        await workspaces.get(session, payload.workspace_id)
    paper = await discovery.add(session, providers, payload.candidate.to_candidate(), settings.pdf_dir)
    if payload.workspace_id is not None:
        await workspaces.add_paper(session, payload.workspace_id, paper.id)
    await request.app.state.arq.enqueue_job("ingest_paper", str(paper.id), _queue_name=INGEST_QUEUE)
    return paper
