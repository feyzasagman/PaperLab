"""M61 enrich_papers: gives library papers that are the arXiv copy of a published paper their published version
(venue, DOI, peer review) from Semantic Scholar's batch endpoint. Only on an explicit call. Never writes the paper's
own doi, venue, files or status, and never a field the owner edited by hand (manual_fields)."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.candidates import s2_triage
from app.core.enrichment import normalize_doi
from app.core.hit_enrichment import CHUNK, batch_with_backoff
from app.core.source_types import ARXIV_DOI_PREFIX, peer_reviewed_of
from app.models.paper import Paper
from app.models.workspace import workspace_papers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperEnrichResult:
    looked_up: int = 0
    filled: int = 0
    not_found: int = 0
    remaining: int = 0
    rate_limited: bool = False


async def _unchecked(session: AsyncSession, ws: uuid.UUID) -> list[Paper]:
    query = (
        select(Paper)
        .join(workspace_papers, workspace_papers.c.paper_id == Paper.id)
        .where(workspace_papers.c.workspace_id == ws, Paper.published_checked_at.is_(None))
        .order_by(Paper.id)
    )
    return list((await session.execute(query)).scalars())


async def papers_to_enrich(session: AsyncSession, ws: uuid.UUID) -> list[Paper]:
    """The workspace's papers Semantic Scholar was not asked about yet and that it can be asked about."""
    return [p for p in await _unchecked(session, ws) if s2_id(p)]


def s2_id(paper: Paper) -> str | None:
    """"ARXIV:<id>" for an arXiv-issued DOI, "DOI:<doi>" for any other, else None (no arXiv id column exists)."""
    doi = (paper.doi or "").strip()
    if not doi:
        return None
    if doi.lower().startswith(ARXIV_DOI_PREFIX):
        return f"ARXIV:{doi[len(ARXIV_DOI_PREFIX):]}"
    return f"DOI:{doi}"


def apply_s2(paper: Paper, answer: dict) -> bool:
    """Sets published_doi, published_venue and peer_reviewed from an S2 paper that is a published version (even over
    "false"), and source_type when empty. Fields in manual_fields are skipped. True when anything changed."""
    source_type, published_doi, published_venue = s2_triage(answer)
    if published_doi and published_doi == normalize_doi(paper.doi or ""):
        published_doi = None  # the paper's own DOI is not its published version's
    if not (published_doi or published_venue):
        return False  # an arXiv-only answer: nothing to learn
    found = {
        "published_doi": published_doi,
        "published_venue": published_venue,
        "peer_reviewed": peer_reviewed_of(source_type) if source_type else None,
    }
    if not paper.source_type:
        found["source_type"] = source_type
    changed = False
    for field, value in found.items():
        if value and value != "unknown" and field not in paper.manual_fields and getattr(paper, field) != value:
            setattr(paper, field, value)
            changed = True
    return changed


async def enrich(session: AsyncSession, http: httpx.AsyncClient, ws: uuid.UUID) -> PaperEnrichResult:
    """Looks the papers up in chunks of CHUNK, one batch call each, committing per chunk. A chunk that fails is
    logged and counted as not found; four 429s in a row stop the job, with the count of papers left."""
    pending = await _unchecked(session, ws)
    papers = [(p, key) for p in pending if (key := s2_id(p))]
    for paper in pending:
        if not s2_id(paper):
            paper.published_checked_at = datetime.now(timezone.utc)  # nothing to ask about: never picked again
    await session.commit()
    looked_up = filled = not_found = 0
    for start in range(0, len(papers), CHUNK):
        chunk = papers[start : start + CHUNK]
        try:
            answers = await batch_with_backoff(http, [key for _, key in chunk])
            if answers is not None and len(answers) != len(chunk):
                raise httpx.DecodingError(f"S2 answered {len(answers)} papers for {len(chunk)} ids")
        except httpx.HTTPError:
            logger.exception("Semantic Scholar batch of %d papers failed; going on", len(chunk))
            not_found += len(chunk)
            continue
        if answers is None:
            remaining = len(papers) - start
            logger.warning("Semantic Scholar kept answering 429; stopping with %d papers left", remaining)
            return PaperEnrichResult(looked_up, filled, not_found, remaining, True)
        looked_up += len(chunk)
        asked_at = datetime.now(timezone.utc)
        for (paper, _), answer in zip(chunk, answers, strict=True):
            paper.published_checked_at = asked_at
            if answer is None:
                not_found += 1
            elif apply_s2(paper, answer):
                filled += 1
        await session.commit()
    return PaperEnrichResult(looked_up, filled, not_found)
