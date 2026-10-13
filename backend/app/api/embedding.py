"""Search: which source it embeds with and switching it (D156–D158), the built-in model's download (D135), the
index it builds and the confirmed re-index."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterable
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict

from app.api.deps import SessionDep, TransportDep
from app.config import settings
from app.core import chat, embedding_index, embedding_sources
from app.core.errors import Conflict, InvalidInput
from app.core.queue import INGEST_QUEUE
from app.db import SessionLocal
from app.providers import embedding, search_model
from app.providers.base import LLMError, LLMUnavailable, ModelNotPulled, WrongDimensions
from app.schemas.llm import ModelName

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/embedding", tags=["embedding"])

# One download at a time. ponytail: a lock per API process, and compose runs one; a second uvicorn worker would need
# a lock file in MODELS_DIR.
_downloading = asyncio.Lock()
ALREADY_RUNNING = "The search model is already downloading."
UNEXPECTED = "The download stopped because of an unexpected error. Try again: it resumes."
NOT_QUEUED = "The search model is ready, but your papers couldn't be queued for search. Re-index them in Settings."
# D158 step 5: the switch is saved, but its papers couldn't be queued. Recorded as the source's error: Try again shows.
NOT_QUEUED_SWITCH = "Search now uses {label}, but your papers couldn't be queued. Press Try again in Settings → Search."
SourceKind = Literal["builtin", "ollama", "openai", "gemini", "openai_compatible"]


class IndexedModelOut(BaseModel):
    model: str
    chunks: int


class SearchSourceOut(BaseModel):
    """The search source without its key (M9's rule). `label`: Built-in, OpenAI, Gemini, or the connection's label."""

    kind: SourceKind
    connection_id: uuid.UUID | None
    connection_label: str | None
    model: str | None
    host: str | None
    is_local: bool
    label: str


class RebuildOut(BaseModel):
    done: int  # papers of the rebuild already on the new source
    total: int


class EmbeddingStatusOut(BaseModel):
    model: str
    chunks: int
    indexed_with: list[IndexedModelOut]
    model_present: bool  # the built-in search model is downloaded
    download_bytes: int  # what Download would fetch now: the model's files, minus what a stopped download left
    unembedded_papers: int  # papers with chunks and no vectors; a finished download queues them (D137)
    papers_needing_search: int  # ready papers too long to send to chat whole (the library notice, P1)
    source: SearchSourceOut
    rebuild: RebuildOut | None  # D156: the app polls every 2 s while it is set
    source_error: str | None  # D155: the last embedding failure, masked
    library_papers: int  # papers with chunks
    library_notes: int
    library_chars: int  # their chunk text and every note's text: the switch dialog's estimate (D157, P3 = A)


class ReindexRequest(BaseModel):
    # Literal[True]: a missing or false flag is a 422, so nothing re-embeds without the explicit confirmation.
    confirm: Literal[True]
    missing_only: bool = False  # Try again: only the papers not yet on the active source (D155)


class ReindexOut(BaseModel):
    papers: int


class SearchSourceIn(BaseModel):
    # extra="forbid": a key sent here is a 422, and the validation handler never echoes it back.
    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    connection_id: uuid.UUID | None = None
    model: ModelName | None = None  # OpenAI: one of embedding_sources.OPENAI_MODELS; a compatible server: any name
    # Literal[True]: nothing is sent to a source and nothing re-embeds without the explicit confirmation.
    confirm: Literal[True]


# SSE payloads of the download: progress (repeated), then done, or error.
class DownloadProgressEvent(BaseModel):
    file: str
    completed: int  # bytes of the whole download, both files
    total: int


class DownloadDoneEvent(BaseModel):
    papers_queued: int


class DownloadErrorEvent(BaseModel):
    detail: str


# The route yields ServerSentEvent, so FastAPI can't see the payload models; listing them puts them in OpenAPI.
DOWNLOAD_RESPONSES = {200: {"model": DownloadProgressEvent | DownloadDoneEvent | DownloadErrorEvent}}


@router.get("")
async def embedding_status(session: SessionDep) -> EmbeddingStatusOut:
    source = await embedding_sources.active(session)
    rebuild = await embedding_index.rebuild(session, source)
    size = await embedding_index.library_size(session)
    return EmbeddingStatusOut(
        **asdict(await embedding_index.status(session, source.name)),
        model_present=search_model.present(settings.models_dir, search_model.SHIPPED),
        download_bytes=search_model.download_bytes(settings.models_dir, search_model.SHIPPED),
        unembedded_papers=len(await embedding_index.unembedded_papers(session)),
        papers_needing_search=await chat.papers_needing_search(session),
        source=SearchSourceOut(**asdict(embedding_sources.view(source))),
        rebuild=None if rebuild is None else RebuildOut(**asdict(rebuild)),
        source_error=source.error,
        library_papers=size.papers,
        library_notes=size.notes,
        library_chars=size.chars,
    )


async def _probe(candidate: embedding_sources.Source, transport) -> None:
    """D158 step 4: one fixed question through the candidate, never library text. A wrong key, a model that isn't
    there or the wrong size refuse the switch, and nothing changes."""
    embedder = await asyncio.to_thread(embedding.build, candidate, transport)
    if embedder is None:  # Built-in without its model: picking it never downloads it (D133)
        raise Conflict("search_model_missing")
    try:
        await embedding.embed_query(embedder, embedding_sources.PROBE)
    except ModelNotPulled:
        raise Conflict("embedding_model_not_pulled") from None
    except WrongDimensions as exc:
        raise InvalidInput(str(exc)) from None
    except (LLMUnavailable, LLMError) as exc:
        logger.warning("search source %s (%s) refused the probe: %s", candidate.label, candidate.host, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.put("/source", status_code=202, responses={200: {"model": ReindexOut}})
async def switch_source(
    payload: SearchSourceIn, request: Request, response: Response, session: SessionDep, transport: TransportDep
) -> ReindexOut:
    """Makes the pick the search source (D158): checks it (422, 404), probes it, saves it and starts a rebuild, then
    queues every paper not yet on its name. 200 with nothing queued for the source in use; 409 search_model_missing,
    embedding_model_not_pulled; 502 the provider's sentence; 503 when the papers couldn't be queued after the save."""
    candidate = await embedding_sources.candidate(session, payload.kind, payload.connection_id, payload.model)
    active = await embedding_sources.active(session)
    if (candidate.kind, candidate.connection_id, candidate.model) == (active.kind, active.connection_id, active.model):
        response.status_code = 200
        return ReindexOut(papers=0)
    await _probe(candidate, transport)
    await embedding_sources.save(session, candidate)
    paper_ids = await embedding_index.papers_to_embed(session, candidate.name)
    try:
        for paper_id in paper_ids:
            await request.app.state.arq.enqueue_job("reembed_paper", str(paper_id), True, _queue_name=INGEST_QUEUE)
    except Exception:
        logger.exception("queueing papers for the search source %s failed", candidate.label)
        message = NOT_QUEUED_SWITCH.format(label=candidate.label)
        await embedding_sources.record_error(session, message)
        raise HTTPException(status_code=503, detail=message) from None
    return ReindexOut(papers=len(paper_ids))


async def download_allowed() -> None:
    """409s before the stream starts: the model is here already, or another download is running."""
    if search_model.present(settings.models_dir, search_model.SHIPPED):
        raise Conflict("model_present")
    if _downloading.locked():
        raise Conflict("download_running")


def download_error(detail: str) -> ServerSentEvent:
    return ServerSentEvent(event="error", data=DownloadErrorEvent(detail=detail))


@router.post(
    "/model",
    response_class=EventSourceResponse,
    responses=DOWNLOAD_RESPONSES,
    dependencies=[Depends(download_allowed)],
)
async def download_model(request: Request, transport: TransportDep) -> AsyncIterable[ServerSentEvent]:
    """Downloads the built-in search model into MODELS_DIR. Events: progress (repeated), then done with how many
    papers were queued for embedding (D137). error replaces done: a damaged file is deleted, anything else that
    arrived is kept for the next try. 409 model_present or download_running before the stream."""
    if _downloading.locked():  # another request started between the check and this stream
        yield download_error(ALREADY_RUNNING)
        return
    async with _downloading:
        try:
            async for progress in search_model.download(settings.models_dir, search_model.SHIPPED, transport):
                yield ServerSentEvent(event="progress", data=DownloadProgressEvent(**asdict(progress)))
        except search_model.DownloadError as exc:
            logger.warning("the search model download stopped: %s (%r)", exc, exc.__cause__)
            yield download_error(str(exc))
            return
        except Exception:
            logger.exception("the search model download failed unexpectedly")
            yield download_error(UNEXPECTED)
            return
    try:
        async with SessionLocal() as session:
            paper_ids = await embedding_index.unembedded_papers(session)
        for paper_id in paper_ids:
            await request.app.state.arq.enqueue_job("reembed_paper", str(paper_id), _queue_name=INGEST_QUEUE)
    except Exception:
        # Without this the client waits for an event that never comes.
        logger.exception("queueing papers for embedding after the search model download failed")
        yield download_error(NOT_QUEUED)
        return
    yield ServerSentEvent(event="done", data=DownloadDoneEvent(papers_queued=len(paper_ids)))


@router.post("/reindex", status_code=202)
async def reindex_library(payload: ReindexRequest, request: Request, session: SessionDep) -> ReindexOut:
    """Queues a re-embed of every paper with chunks, or with missing_only (Settings' Try again) only those not yet on
    the active source. Both clear the last error and start a rebuild (D156). Chunk ids stay, so answers and notes keep
    their sources."""
    source = await embedding_sources.active(session)
    await embedding_sources.save(session, source)
    if payload.missing_only:
        paper_ids = await embedding_index.papers_to_embed(session, source.name)
    else:
        paper_ids = await embedding_index.indexed_papers(session)
    for paper_id in paper_ids:
        await request.app.state.arq.enqueue_job(
            "reembed_paper", str(paper_id), payload.missing_only, _queue_name=INGEST_QUEUE
        )
    return ReindexOut(papers=len(paper_ids))
