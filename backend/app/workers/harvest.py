"""Periodic metadata harvesting for sources with no live search API (M32 batch 6 phase A: IACR ePrint
only; a later phase adds bioRxiv/medRxiv onto this same infrastructure). Each harvest source gets one
HarvestCursor row tracking its own last-synced point, and one ARQ cron job (registered in
workers/settings.py) that pages through everything new since then and upserts it into external_refs --
the same generic store every live source's own candidates already land in.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.candidates import Candidate, from_biorxiv_entry, from_iacr_eprint
from app.db import SessionLocal
from app.models import ExternalRef
from app.models.harvest import HarvestCursor
from app.providers import biorxiv, iacr_eprint

logger = logging.getLogger(__name__)

# ARQ's own default job_timeout is 300s; a first-ever harvest can page through roughly 57 pages of 500
# (confirmed live: IACR's own full archive is 28,110 records) -- generous, matching
# workers/workspace_search.py's own SEARCH_RUN_JOB_TIMEOUT precedent for a similarly long-running job.
HARVEST_JOB_TIMEOUT = 1800
# Bounds one job run's own page count, the same backstop shape SEARCH_RUN's own MAX_BATCH_ITERATIONS
# uses -- a first-ever harvest needs ~57 pages; 200 leaves headroom without looping forever on a bug.
MAX_HARVEST_PAGES = 200

# bioRxiv's own full history is 487,627 records and medRxiv's is 112,661 (confirmed live 2026-10-10) --
# mirroring the entire platform history on the first-ever run, the way IACR's comparatively small
# 28,110-record archive was, is not practical (combined ~20,000 pages at 30/page). BIORXIV_BOOTSTRAP_DATE
# bounds the FIRST-EVER sync for either source to a fixed, deliberately recent window -- confirmed live
# this still covers 185,788 (bioRxiv) + 50,470 (medRxiv) = 236,258 records on that one run. Every run
# AFTER the first picks up from the advancing cursor, independent of this constant -- it only matters
# once, for a never-before-synced source.
BIORXIV_BOOTSTRAP_DATE = "2024-01-01"
# At 30 records/page, bioRxiv's own first-ever bootstrap window needs ~6,193 pages; medRxiv's needs
# ~1,683. Generous headroom over the larger of the two, the same proportional logic IACR's own
# MAX_HARVEST_PAGES (200, for a ~57-page real need) used.
BIORXIV_MAX_HARVEST_PAGES = 8000
# IACR's own real full harvest ran 57 pages in 47.21s (~0.82s/page, confirmed live). Scaled to bioRxiv's
# own larger first-ever page count (~6,193) at that same real rate: ~5,078s (~85 minutes) -- this budget
# gives that run comfortable headroom without being absurd.
BIORXIV_HARVEST_JOB_TIMEOUT = 7200


async def harvest_ingest_candidate(session: AsyncSession, candidate: Candidate) -> None:
    """Find this harvest source's own existing row by its own external_ids key, or create one. Not
    workspace_search.py's own _find_or_create_external_ref -- that function resolves conflicts between
    several different LIVE sources' own concurrent results for the SAME query, a different problem from
    recording one periodic source's own harvested finding."""
    source, eprint_id = next(iter(candidate.external_ids.items()))
    existing = (
        await session.execute(select(ExternalRef).where(ExternalRef.external_ids[source].astext == eprint_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.title = candidate.title
        existing.authors = candidate.authors
        existing.abstract = candidate.abstract
        existing.year = candidate.year
        return
    session.add(
        ExternalRef(
            title=candidate.title, authors=candidate.authors, year=candidate.year, abstract=candidate.abstract,
            external_ids=dict(candidate.external_ids), sources=list(candidate.sources),
        )
    )


async def _sync_iacr_eprint(session: AsyncSession) -> None:
    cursor = await session.get(HarvestCursor, "iacr_eprint")
    if cursor is None:
        cursor = HarvestCursor(source="iacr_eprint", last_synced_at=None)
        session.add(cursor)
    from_date = cursor.last_synced_at.strftime("%Y-%m-%d") if cursor.last_synced_at else "1996-01-01"
    until_date = datetime.now(UTC).strftime("%Y-%m-%d")

    http = iacr_eprint.new_client()
    try:
        token: str | None = None
        for _ in range(MAX_HARVEST_PAGES):
            entries, token = (
                await iacr_eprint.fetch_page(http, resumption_token=token)
                if token
                else await iacr_eprint.fetch_page(http, from_date=from_date, until_date=until_date)
            )
            for entry in entries:
                await harvest_ingest_candidate(session, from_iacr_eprint(entry))
            await session.flush()
            if token is None:
                break
        else:
            # The loop exhausted its page budget with more pages still pending -- raise rather than
            # silently truncate the harvest; harvest_iacr_eprint's own except/rollback then keeps the
            # cursor from advancing, so the next run resumes from the last successfully-finished point
            # instead of losing the un-fetched remainder.
            raise RuntimeError(
                f"IACR ePrint harvest hit MAX_HARVEST_PAGES ({MAX_HARVEST_PAGES}) with more pages pending"
            )
    finally:
        await http.aclose()

    cursor.last_synced_at = datetime.strptime(until_date, "%Y-%m-%d").replace(tzinfo=UTC)


async def harvest_iacr_eprint(ctx: dict) -> None:
    async with SessionLocal() as session:
        try:
            await _sync_iacr_eprint(session)
            await session.commit()
        except Exception:
            logger.exception("IACR ePrint harvest failed")
            await session.rollback()
            raise


async def _sync_biorxiv_source(session: AsyncSession, server: str) -> None:
    cursor = await session.get(HarvestCursor, server)
    if cursor is None:
        cursor = HarvestCursor(source=server, last_synced_at=None)
        session.add(cursor)
    from_date = cursor.last_synced_at.strftime("%Y-%m-%d") if cursor.last_synced_at else BIORXIV_BOOTSTRAP_DATE
    until_date = datetime.now(UTC).strftime("%Y-%m-%d")

    http = biorxiv.new_client()
    try:
        page_cursor = 0
        for _ in range(BIORXIV_MAX_HARVEST_PAGES):
            entries, next_page_cursor = await biorxiv.fetch_page(
                http, server, from_date=from_date, until_date=until_date, cursor=page_cursor
            )
            for entry in entries:
                if not entry.get("doi"):
                    logger.warning("%s harvest: skipping a record with no DOI", server)
                    continue
                await harvest_ingest_candidate(session, from_biorxiv_entry(entry, server=server))
            await session.flush()
            if next_page_cursor is None:
                break
            page_cursor = next_page_cursor
        else:
            # Same defensive stance as _sync_iacr_eprint: raise rather than silently truncate, so the
            # transaction rolls back and the cursor stays put -- the next run resumes from the last
            # successfully-finished point instead of losing the un-fetched remainder.
            raise RuntimeError(
                f"{server} harvest hit BIORXIV_MAX_HARVEST_PAGES ({BIORXIV_MAX_HARVEST_PAGES}) "
                "with more pages pending"
            )
    finally:
        await http.aclose()

    cursor.last_synced_at = datetime.strptime(until_date, "%Y-%m-%d").replace(tzinfo=UTC)


async def harvest_biorxiv(ctx: dict) -> None:
    async with SessionLocal() as session:
        try:
            await _sync_biorxiv_source(session, "biorxiv")
            await session.commit()
        except Exception:
            logger.exception("bioRxiv harvest failed")
            await session.rollback()
            raise


async def harvest_medrxiv(ctx: dict) -> None:
    async with SessionLocal() as session:
        try:
            await _sync_biorxiv_source(session, "medrxiv")
            await session.commit()
        except Exception:
            logger.exception("medRxiv harvest failed")
            await session.rollback()
            raise
