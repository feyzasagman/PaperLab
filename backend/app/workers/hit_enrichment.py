"""ARQ job: enrich_hits (M59), the Semantic Scholar backfill of triage data and missing abstracts."""

import logging
import uuid
from dataclasses import replace

from app.config import settings
from app.core import hit_enrichment, paper_sources
from app.db import SessionLocal
from app.providers import arxiv, discovery_fake, semantic_scholar

logger = logging.getLogger(__name__)

ENRICH_JOB_TIMEOUT = 3600


async def _arxiv_fallback(session, refs, transport) -> int:
    if transport is None and settings.discovery_provider == "fake":
        transport = discovery_fake.transport()
    async with arxiv.new_client(transport=transport) as http:
        return (await hit_enrichment.arxiv_abstracts(session, http, refs)).filled


async def enrich_hits(ctx: dict, workspace_id: str, run_id: str | None = None) -> None:
    """Never raises for S2 or arXiv trouble: a rate-limited or failing chunk ends up in the logged result.
    ctx["transport"] and ctx["arxiv_transport"] are a test's MockTransports."""
    async with SessionLocal() as session:
        sources = await paper_sources.get(session)
        transport = ctx.get("transport")
        if transport is None and settings.discovery_provider == "fake":
            transport = discovery_fake.transport()
        # No RetryOn429: hit_enrichment waits on Retry-After itself.
        async with semantic_scholar.new_client(
            email=sources.contact_email,
            api_key=sources.api_keys.get("semantic_scholar"),
            transport=transport,
            retry_429=False,
        ) as http:
            ws, run = uuid.UUID(workspace_id), uuid.UUID(run_id) if run_id else None
            pending = await hit_enrichment.refs_to_enrich(session, ws, run)
            outcome = await hit_enrichment.enrich(session, http, ws, run)
        if not outcome.rate_limited:  # leave arXiv for the next call when S2 is busy
            outcome = replace(outcome, arxiv_filled=await _arxiv_fallback(session, pending, ctx.get("arxiv_transport")))
        outcome = replace(outcome, suggestions_cleared=await hit_enrichment.clear_stale_no_abstract(session, ws))
    logger.info("enrich_hits for %s: %s", workspace_id, outcome)
