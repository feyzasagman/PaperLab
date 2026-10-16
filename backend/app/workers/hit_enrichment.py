"""ARQ job: enrich_hits (M59), the Semantic Scholar backfill of triage data and missing abstracts."""

import logging
import uuid
from dataclasses import replace

from app.config import settings
from app.core import hit_enrichment, paper_sources
from app.db import SessionLocal
from app.providers import discovery_fake, semantic_scholar

logger = logging.getLogger(__name__)

ENRICH_JOB_TIMEOUT = 3600


async def enrich_hits(ctx: dict, workspace_id: str, run_id: str | None = None) -> None:
    """Never raises for S2 trouble: a rate-limited or failing chunk ends up in the logged result. ctx["transport"] is
    a test's MockTransport."""
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
            ws = uuid.UUID(workspace_id)
            outcome = await hit_enrichment.enrich(session, http, ws, uuid.UUID(run_id) if run_id else None)
        outcome = replace(outcome, suggestions_cleared=await hit_enrichment.clear_stale_no_abstract(session, ws))
    logger.info("enrich_hits for %s: %s", workspace_id, outcome)
