"""ARQ job: enrich_papers (M61), the Semantic Scholar lookup of library papers' published versions."""

import logging
import uuid

from app.config import settings
from app.core import paper_enrichment, paper_sources
from app.db import SessionLocal
from app.providers import discovery_fake, semantic_scholar

logger = logging.getLogger(__name__)

ENRICH_PAPERS_TIMEOUT = 3600


async def enrich_papers(ctx: dict, workspace_id: str) -> None:
    """Never raises for S2 trouble: a rate-limited or failing chunk ends up in the logged result.
    ctx["transport"] is a test's MockTransport."""
    async with SessionLocal() as session:
        sources = await paper_sources.get(session)
        transport = ctx.get("transport")
        if transport is None and settings.discovery_provider == "fake":
            transport = discovery_fake.transport()
        # No RetryOn429: hit_enrichment's helper waits on Retry-After itself.
        async with semantic_scholar.new_client(
            email=sources.contact_email,
            api_key=sources.api_keys.get("semantic_scholar"),
            transport=transport,
            retry_429=False,
        ) as http:
            outcome = await paper_enrichment.enrich(session, http, uuid.UUID(workspace_id))
    logger.info("enrich_papers for %s: %s", workspace_id, outcome)
