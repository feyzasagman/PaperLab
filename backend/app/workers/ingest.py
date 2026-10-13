import asyncio
import logging
import uuid
from contextlib import AsyncExitStack

from app.core import embedding_index, embedding_sources, enrichment, paper_sources, papers
from app.core.chunking import chunk_blocks
from app.db import SessionLocal
from app.models import PaperStatus
from app.providers import crossref, embedding, openalex
from app.providers.base import LLMError, LLMUnavailable
from app.providers.extraction import ExtractedDoc, extract

logger = logging.getLogger(__name__)

# arq's own default, set on IngestWorkerSettings so the two can't drift. arq never runs a job past it, so the last try
# ends the paper failed instead of starting again: a paper that stops the worker every time would otherwise sit
# mid-ingest forever, as 67 did when ten ingests at once ran the worker out of memory (2026-10-13).
MAX_TRIES = 5
INTERRUPTED = (
    "Ingesting stopped before it finished {times} times (the worker restarted or ran out of memory). "
    "Re-ingest to try again."
)


async def _enrich(session, transport, paper_id: uuid.UUID, doc: ExtractedDoc) -> None:
    """Asks OpenAlex only while it is on in Settings → Paper sources, read here so a change applies to the next ingest
    without a restart (P5). Never fatal (addendum §6d): on any error the paper keeps whatever was saved before the
    error and still reaches ready."""
    try:
        hints = enrichment.pdf_hints(doc.first_page_text, doc.metadata)
        sources = await paper_sources.get(session)
        async with AsyncExitStack() as stack:
            http = cr = None
            if sources.enabled["openalex"]:
                key = sources.api_keys["openalex"]
                http = await stack.enter_async_context(
                    openalex.new_client(email=sources.contact_email, transport=transport, api_key=key)
                )
            if sources.enabled["crossref"]:  # one polite-pool request at most, only for a paper OpenAlex left untyped
                cr = await stack.enter_async_context(
                    crossref.new_client(email=sources.contact_email, transport=transport)
                )
            await enrichment.enrich_paper(session, http, paper_id, hints, crossref_http=cr)
    except Exception:
        logger.exception("enrichment failed for %s; the paper stays usable", paper_id)
        await session.rollback()


async def search_embedder(ctx: dict, session):
    """The embedder for this job: a test's fake in ctx["embedder"], else the active search source's, read now so a
    switch applies to the next job without a restart (D151). ctx["transport"] is a test's MockTransport. None while
    Built-in is the source and its model isn't downloaded (D136)."""
    if "embedder" in ctx:
        return ctx["embedder"]
    source = await embedding_sources.active(session)
    return await asyncio.to_thread(embedding.build, source, ctx.get("transport"))


async def _embed(session, pid: uuid.UUID, embedder) -> bool:
    """Embeds the paper's chunks and writes them if `embedder` is still the search source. A failing source never
    fails a paper: the old vectors stay and the error is kept for Settings' Try again (D155). True when written."""
    chunks = await papers.list_chunks(session, pid)
    try:
        vectors = await embedding.embed_documents(embedder, [c.text for c in chunks])
    except (LLMUnavailable, LLMError) as exc:
        logger.warning("embedding %s with %s failed: %s", pid, embedder.label, exc)
        await embedding_sources.record_error(session, str(exc))
        return False
    # ponytail: a switch landing between this check and the commit leaves one paper on the old source; Try again
    # fixes it.
    if await embedding_sources.active_name(session) != embedder.name:
        logger.info("the search source changed while embedding %s; the newer job embeds it", pid)
        return False
    await papers.set_embeddings(session, [c.id for c in chunks], vectors, embedder.name)
    return True


async def ingest_paper(ctx: dict, paper_id: str) -> None:
    """uploaded -> extracting -> chunking -> embedding -> enriching -> ready, or failed with status_error.

    Idempotent: chunks, vectors, authorships and topics are replaced wholesale, so re-run it freely. With no search
    model the embedding stage is skipped: the paper is ready to read and note, and a finished download embeds it (D137).
    A failing search source leaves the paper ready without vectors (D155). Tests put a fake model in
    ctx["embedder"] and an httpx transport in ctx["transport"].
    """
    pid = uuid.UUID(paper_id)
    if ctx.get("job_try", 1) >= MAX_TRIES:
        async with SessionLocal() as session:
            await papers.set_status(session, pid, PaperStatus.FAILED, error=INTERRUPTED.format(times=MAX_TRIES - 1))
        logger.warning("ingest of %s stopped %d times; marked failed", pid, MAX_TRIES - 1)
        return
    async with SessionLocal() as session:
        paper = await papers.get_paper(session, pid)
        try:
            await papers.set_status(session, pid, PaperStatus.EXTRACTING)
            doc = await asyncio.to_thread(extract, paper.file_path)
            # extract() can take seconds; re-read the lock right before deciding, in case a PATCH landed meanwhile.
            await session.refresh(paper, ["openalex_id", "manual_fields"])

            # Enriched or corrected titles beat the font heuristic; don't overwrite them on re-runs.
            keep = paper.openalex_id is not None or "title" in paper.manual_fields
            title = {"title": doc.title} if doc.title and not keep else {}
            await papers.set_status(session, pid, PaperStatus.CHUNKING, page_count=doc.page_count, **title)
            drafts = chunk_blocks(doc.blocks)
            await papers.replace_chunks(session, pid, drafts)

            embedder = await search_embedder(ctx, session)
            if embedder is None:
                logger.info("no search model: %s is ready without vectors", pid)
            else:
                await papers.set_status(session, pid, PaperStatus.EMBEDDING)
                await _embed(session, pid, embedder)

            await papers.set_status(session, pid, PaperStatus.ENRICHING)
            await _enrich(session, ctx.get("transport"), pid, doc)
            # Don't read paper's ORM attributes past this point: _enrich's rollback (on the failure path) expires
            # them, and an async lazy load outside an awaited call raises MissingGreenlet, not a clean re-fetch.

            await papers.set_status(session, pid, PaperStatus.READY)
            logger.info("ingested %s: %d pages, %d chunks", pid, doc.page_count, len(drafts))
        except Exception as exc:
            logger.exception("ingest failed for %s", pid)
            await session.rollback()
            await papers.set_status(session, pid, PaperStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


async def reembed_paper(ctx: dict, paper_id: str, missing_only: bool = False) -> None:
    """Embeds a paper's chunks again with the search source, in place: chunk ids stay, so saved answers and notes keep
    their sources. A re-index queues one per paper; a switch and Try again queue them with missing_only, which skips a
    paper already on the source, so a double click or an earlier queued job never pays a cloud twice (D155). A
    finished model download queues one per paper without vectors (D137). With no search model it does nothing."""
    pid = uuid.UUID(paper_id)
    async with SessionLocal() as session:
        embedder = await search_embedder(ctx, session)
        if embedder is None:
            logger.info("no search model: re-embedding %s skipped", pid)
            return
        if missing_only and not await embedding_index.papers_to_embed(session, embedder.name, [pid]):
            logger.info("%s is already embedded with %s", pid, embedder.name)
            return
        if await _embed(session, pid, embedder):
            logger.info("re-embedded %s with %s", pid, embedder.name)
