"""M59 enrich_hits: fills source_type, published_doi, published_venue and a missing abstract on the refs of a
workspace's search hits from Semantic Scholar's batch endpoint. Only on an explicit call; never overwrites a value."""

import logging
import uuid
from asyncio import sleep
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.candidates import s2_triage
from app.core.source_types import ARXIV_DOI_PREFIX
from app.models.references import ExternalRef
from app.models.workspace_search import WorkspaceSearchHit
from app.providers import semantic_scholar

logger = logging.getLogger(__name__)

CHUNK = 500  # S2's batch limit
RATE_LIMIT_WAITS = (2.0, 5.0, 15.0)  # when a 429 has no Retry-After, one per retry
MAX_WAIT = 60.0
MAX_429 = 4  # consecutive 429s on one chunk before the job stops


@dataclass(frozen=True)
class EnrichResult:
    looked_up: int = 0
    filled: int = 0
    not_found: int = 0
    remaining: int = 0
    rate_limited: bool = False


async def refs_to_enrich(session: AsyncSession, ws: uuid.UUID, run_id: uuid.UUID | None) -> list[ExternalRef]:
    """Refs of the workspace's hits (optionally one run's) with a DOI or arXiv id and no source_type or
    abstract, that Semantic Scholar was not asked about yet."""
    hits = select(WorkspaceSearchHit.external_ref_id).where(WorkspaceSearchHit.workspace_id == ws)
    if run_id is not None:
        hits = hits.where(WorkspaceSearchHit.run_id == run_id)
    wanted = ExternalRef.doi.is_not(None) | ExternalRef.external_ids["arxiv"].astext.is_not(None)
    missing = ExternalRef.source_type.is_(None) | (func.btrim(func.coalesce(ExternalRef.abstract, "")) == "")
    query = (
        select(ExternalRef)
        .where(ExternalRef.id.in_(hits), wanted, missing, ExternalRef.triage_checked_at.is_(None))
        .order_by(ExternalRef.id)
    )
    return list((await session.execute(query)).scalars())


def s2_id(ref: ExternalRef) -> str | None:
    """"DOI:<doi>", else "ARXIV:<id>". An arXiv-issued DOI is no DOI to S2: use the arXiv id."""
    doi = (ref.doi or "").strip()
    if doi and not doi.lower().startswith(ARXIV_DOI_PREFIX):
        return f"DOI:{doi}"
    arxiv = (ref.external_ids or {}).get("arxiv")
    return f"ARXIV:{arxiv}" if arxiv else None


def apply_s2(ref: ExternalRef, paper: dict) -> bool:
    """Fills the ref's empty triage fields and abstract from an S2 paper. True when anything changed."""
    source_type, published_doi, published_venue = s2_triage(paper)
    found = {
        "source_type": source_type,
        "published_doi": published_doi,
        "published_venue": published_venue,
        "abstract": (paper.get("abstract") or "").strip() or None,
    }
    changed = False
    for field, value in found.items():
        if value and not (getattr(ref, field) or "").strip():
            setattr(ref, field, value)
            changed = True
    return changed


def _wait_seconds(response: httpx.Response, attempt: int) -> float:
    try:
        asked = float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        asked = RATE_LIMIT_WAITS[min(attempt, len(RATE_LIMIT_WAITS) - 1)]
    return min(max(asked, 0.0), MAX_WAIT)


async def _batch_with_backoff(http: httpx.AsyncClient, ids: list[str]) -> list[dict | None] | None:
    """The chunk's answers, or None after MAX_429 consecutive 429s. Other HTTP errors raise."""
    for attempt in range(MAX_429):
        try:
            return await semantic_scholar.batch(http, ids)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            if attempt < MAX_429 - 1:
                await sleep(_wait_seconds(exc.response, attempt))
    return None


async def enrich(
    session: AsyncSession, http: httpx.AsyncClient, ws: uuid.UUID, run_id: uuid.UUID | None
) -> EnrichResult:
    """Looks the refs up in chunks of CHUNK, one batch call each, committing per chunk. A chunk that fails is logged
    and counted as not found; four 429s in a row stop the job, with the count of refs left."""
    pending = await refs_to_enrich(session, ws, run_id)
    refs = [(ref, key) for ref in pending if (key := s2_id(ref))]
    for ref in pending:
        if not s2_id(ref):
            ref.triage_checked_at = datetime.now(timezone.utc)  # nothing to ask about: never picked again
    await session.commit()
    looked_up = filled = not_found = 0
    for start in range(0, len(refs), CHUNK):
        chunk = refs[start : start + CHUNK]
        try:
            answers = await _batch_with_backoff(http, [key for _, key in chunk])
            if answers is not None and len(answers) != len(chunk):
                raise httpx.DecodingError(f"S2 answered {len(answers)} papers for {len(chunk)} ids")
        except httpx.HTTPError:
            logger.exception("Semantic Scholar batch of %d refs failed; going on", len(chunk))
            not_found += len(chunk)
            continue
        if answers is None:
            remaining = len(refs) - start
            logger.warning("Semantic Scholar kept answering 429; stopping with %d refs left", remaining)
            return EnrichResult(looked_up, filled, not_found, remaining, True)
        looked_up += len(chunk)
        asked_at = datetime.now(timezone.utc)
        for (ref, _), paper in zip(chunk, answers, strict=True):
            ref.triage_checked_at = asked_at
            if paper is None:
                not_found += 1
            elif apply_s2(ref, paper):
                filled += 1
        await session.commit()
    return EnrichResult(looked_up, filled, not_found)
