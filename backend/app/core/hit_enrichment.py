"""M59 enrich_hits: fills source_type, published_doi, published_venue and a missing abstract on the refs of a
workspace's search hits from Semantic Scholar's batch endpoint. Only on an explicit call; never overwrites a value."""

import logging
import re
import uuid
from asyncio import sleep
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.candidates import ARXIV_ID, ARXIV_VERSION, s2_triage
from app.core.source_types import ARXIV_DOI_PREFIX
from app.models.references import ExternalRef
from app.models.workspace_search import WorkspaceSearchHit
from app.providers import arxiv, semantic_scholar

logger = logging.getLogger(__name__)

CHUNK = 500  # S2's batch limit
RATE_LIMIT_WAITS = (2.0, 5.0, 15.0)  # when a 429 has no Retry-After, one per retry
MAX_WAIT = 60.0
MAX_429 = 4  # consecutive 429s on one chunk before the job stops
ARXIV_CHUNK = 100  # arXiv's id_list limit
ARXIV_PAUSE = 3.0  # arXiv asks for one request every 3 seconds
ARXIV_BUSY = (429, 503)
_ARXIV_FULL = re.compile(ARXIV_ID)


@dataclass(frozen=True)
class EnrichResult:
    looked_up: int = 0
    filled: int = 0
    not_found: int = 0
    remaining: int = 0
    rate_limited: bool = False
    suggestions_cleared: int = 0
    arxiv_filled: int = 0
    arxiv_rate_limited: bool = False


def _hit_refs(ws: uuid.UUID, run_id: uuid.UUID | None):
    hits = select(WorkspaceSearchHit.external_ref_id).where(WorkspaceSearchHit.workspace_id == ws)
    return hits if run_id is None else hits.where(WorkspaceSearchHit.run_id == run_id)


async def refs_for_arxiv(session: AsyncSession, ws: uuid.UUID, run_id: uuid.UUID | None) -> list[ExternalRef]:
    """Refs of the workspace's hits (optionally one run's) with an arXiv id and no abstract. Unlike refs_to_enrich
    there is no triage_checked_at filter, so what an earlier arXiv stop left is asked again.
    ponytail: an arXiv id arXiv doesn't know stays queued; mark such refs if that ever shows up."""
    query = (
        select(ExternalRef)
        .where(
            ExternalRef.id.in_(_hit_refs(ws, run_id)),
            ExternalRef.external_ids["arxiv"].astext.is_not(None),
            func.btrim(func.coalesce(ExternalRef.abstract, "")) == "",
        )
        .order_by(ExternalRef.id)
    )
    return [r for r in (await session.execute(query)).scalars() if _arxiv_id(r)]  # the count and the pass agree


async def refs_to_enrich(session: AsyncSession, ws: uuid.UUID, run_id: uuid.UUID | None) -> list[ExternalRef]:
    """Refs of the workspace's hits (optionally one run's) with a DOI or arXiv id and no source_type or
    abstract, that Semantic Scholar was not asked about yet."""
    hits = _hit_refs(ws, run_id)
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
    arxiv_id = (paper.get("externalIds") or {}).get("ArXiv")
    if arxiv_id and not (ref.external_ids or {}).get("arxiv"):
        ref.external_ids = {**(ref.external_ids or {}), "arxiv": arxiv_id}  # a new dict, so SQLAlchemy sees it
    return changed


def _wait_seconds(response: httpx.Response, attempt: int) -> float:
    try:
        asked = float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        asked = RATE_LIMIT_WAITS[min(attempt, len(RATE_LIMIT_WAITS) - 1)]
    return min(max(asked, 0.0), MAX_WAIT)


async def batch_with_backoff(http: httpx.AsyncClient, ids: list[str]) -> list[dict | None] | None:
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
            answers = await batch_with_backoff(http, [key for _, key in chunk])
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


@dataclass(frozen=True)
class ArxivResult:
    asked: int = 0
    filled: int = 0
    rate_limited: bool = False


def _arxiv_id(ref: ExternalRef) -> str | None:
    found = ARXIV_VERSION.sub("", ((ref.external_ids or {}).get("arxiv") or "").strip().lower())
    return found if _ARXIV_FULL.fullmatch(found) else None  # one malformed id would make arXiv reject the chunk


async def _arxiv_with_backoff(http: httpx.AsyncClient, ids: list[str]) -> list[dict] | None:
    """The chunk's entries, or None after MAX_429 consecutive 429/503s. Other HTTP errors raise."""
    for attempt in range(MAX_429):
        try:
            return await arxiv.get_many(http, ids)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in ARXIV_BUSY:
                raise
            if attempt < MAX_429 - 1:
                await sleep(_wait_seconds(exc.response, attempt))
    return None


async def _arxiv_one_by_one(http: httpx.AsyncClient, ids: list[str]) -> list[dict] | None:
    """After a 400 for the whole chunk: arXiv rejects the chunk if any id is bad, so ask for each id alone. Same
    429/503 waits as a chunk; None once an id is throttled MAX_429 times in a row."""
    found: list[dict] = []
    for key in ids:
        await sleep(ARXIV_PAUSE)
        try:
            entries = await _arxiv_with_backoff(http, [key])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:  # 400: arXiv can't read this id; skip it
                logger.warning("arXiv answered %d for %s; going on", exc.response.status_code, key)
            continue
        except httpx.HTTPError:
            logger.exception("arXiv lookup of %s failed; going on", key)
            continue
        if entries is None:
            return None
        found.extend(entries)
    return found


async def arxiv_abstracts(
    session: AsyncSession, http: httpx.AsyncClient, refs: list[ExternalRef]
) -> ArxivResult:
    """Fills the empty abstracts of refs that have an arXiv id, ARXIV_CHUNK ids per request, ARXIV_PAUSE seconds
    apart, committing per chunk. A chunk that fails is logged and skipped; four 429/503s in a row stop it."""
    wanted = [(r, key) for r in refs if not (r.abstract or "").strip() and (key := _arxiv_id(r))]
    asked = filled = 0
    for start in range(0, len(wanted), ARXIV_CHUNK):
        if start:
            await sleep(ARXIV_PAUSE)
        chunk = wanted[start : start + ARXIV_CHUNK]
        try:
            entries = await _arxiv_with_backoff(http, [key for _, key in chunk])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                logger.exception("arXiv lookup of %d ids failed; going on", len(chunk))
                continue
            entries = await _arxiv_one_by_one(http, [key for _, key in chunk])
        except httpx.HTTPError:
            logger.exception("arXiv lookup of %d ids failed; going on", len(chunk))
            continue
        if entries is None:
            logger.warning("arXiv kept answering 429/503; stopping with %d refs left", len(wanted) - start)
            return ArxivResult(asked, filled, True)
        asked += len(chunk)
        abstracts = {e["arxiv_id"]: e["abstract"] for e in entries if e["abstract"]}
        for ref, key in chunk:
            if key in abstracts:
                ref.abstract = abstracts[key]
                filled += 1
        await session.commit()
    return ArxivResult(asked, filled)


SUGGESTION_COLUMNS = (
    "suggestion", "suggestion_reason", "suggestion_note", "suggestion_model", "suggested_at", "suggestion_relevance",
    "suggestion_quotes", "suggestion_confidence", "suggestion_flags", "suggestion_prompt_version",
)


async def clear_stale_no_abstract(session: AsyncSession, ws: uuid.UUID) -> int:
    """Clears the 'no abstract' suggestion of undecided hits whose ref has an abstract by now, so pre-screening takes
    them up again. Decided hits and real verdicts are left alone. Returns the number of hits cleared."""
    has_abstract = select(ExternalRef.id).where(func.btrim(func.coalesce(ExternalRef.abstract, "")) != "")
    cleared = await session.execute(
        update(WorkspaceSearchHit)
        .where(
            WorkspaceSearchHit.workspace_id == ws,
            WorkspaceSearchHit.stage1_status.is_(None),
            WorkspaceSearchHit.suggestion_flags.contains(["no_abstract"]),
            WorkspaceSearchHit.external_ref_id.in_(has_abstract),
        )
        .values({column: null() for column in SUGGESTION_COLUMNS})  # null(): JSON null would not be SQL NULL
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return cleared.rowcount
