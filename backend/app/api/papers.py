import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import DiscoveryDep, SessionDep
from app.config import settings
from app.core import discovery, enrichment, papers, workspaces
from app.core.queue import INGEST_QUEUE
from app.models import PaperStatus
from app.schemas.discovery import CandidateOut
from app.schemas.papers import ChunkOut, PaperOut, PaperUpdate, ReadingIn

router = APIRouter(prefix="/api/papers", tags=["papers"])


async def _enqueue_ingest(request: Request, paper_id: uuid.UUID) -> None:
    await request.app.state.arq.enqueue_job("ingest_paper", str(paper_id), _queue_name=INGEST_QUEUE)


@router.post("", status_code=201)
async def upload_paper(
    file: UploadFile,
    request: Request,
    session: SessionDep,
    workspace_id: Annotated[uuid.UUID | None, Form()] = None,
) -> PaperOut:
    """workspace_id: also add the new paper to this workspace (404 before anything is stored if it's unknown)."""
    if workspace_id is not None:
        await workspaces.get(session, workspace_id)
    paper = await papers.create_paper(session, file.filename or "untitled.pdf", await file.read(), settings.pdf_dir)
    if workspace_id is not None:
        await workspaces.add_paper(session, workspace_id, paper.id)
    await _enqueue_ingest(request, paper.id)
    return paper


@router.get("")
async def list_papers(session: SessionDep) -> list[PaperOut]:
    return await papers.list_papers(session)


@router.get("/{paper_id}")
async def get_paper(paper_id: uuid.UUID, session: SessionDep) -> PaperOut:
    return await papers.get_paper(session, paper_id)


@router.patch("/{paper_id}")
async def correct_paper(paper_id: uuid.UUID, payload: PaperUpdate, session: SessionDep) -> PaperOut:
    return await enrichment.correct_metadata(session, paper_id, payload.model_dump(exclude_unset=True))


@router.put("/{paper_id}/reading")
async def set_reading(paper_id: uuid.UUID, payload: ReadingIn, session: SessionDep) -> PaperOut:
    """The reader's passes finished and decision (D119). Only the fields sent change. 404 unknown paper; 422 for an
    empty body, a level outside 0–3, a null level, an unknown decision or any other field."""
    return await papers.set_reading(session, paper_id, payload.model_dump(exclude_unset=True))


@router.delete("/{paper_id}", status_code=204)
async def delete_paper(paper_id: uuid.UUID, session: SessionDep) -> Response:
    await papers.delete_paper(session, paper_id)
    return Response(status_code=204)


@router.get("/{paper_id}/file", response_class=FileResponse)
async def get_paper_file(paper_id: uuid.UUID, session: SessionDep) -> FileResponse:
    return FileResponse(await papers.get_paper_file(session, paper_id), media_type="application/pdf")


@router.get("/{paper_id}/similar")
async def similar_papers(paper_id: uuid.UUID, session: SessionDep, providers: DiscoveryDep) -> list[CandidateOut]:
    """Papers like this one, from Semantic Scholar. 409 when it's off in Settings, doesn't know the paper or is busy."""
    return await discovery.similar(session, providers, await papers.get_paper(session, paper_id))


@router.get("/{paper_id}/chunks")
async def list_chunks(paper_id: uuid.UUID, session: SessionDep, page: int | None = None) -> list[ChunkOut]:
    return await papers.list_chunks(session, paper_id, page)


@router.post("/{paper_id}/reingest", status_code=202)
async def reingest_paper(paper_id: uuid.UUID, request: Request, session: SessionDep) -> PaperOut:
    paper = await papers.get_paper(session, paper_id)
    # Flips the status before enqueueing, so a chat request racing the worker sees `uploaded`
    # (paper_not_ready) instead of a `ready` paper whose chunks are mid-replacement.
    await papers.set_status(session, paper.id, PaperStatus.UPLOADED)
    await session.refresh(paper)
    await _enqueue_ingest(request, paper.id)
    return paper
