"""Fills source_type for papers that have none: from the stored OpenAlex type, else from Crossref.

Run once in the api container: `python -m app.scripts.backfill_source_types [--all]`. Paced (Crossref's polite pool
allows about 3 requests a second), idempotent, and never touches a source type the owner set by hand.
"""

import argparse
import asyncio
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import enrichment, paper_sources
from app.db import SessionLocal
from app.models import Paper
from app.providers import crossref

DELAY = 0.4


async def run(
    session: AsyncSession,
    http: httpx.AsyncClient | None,
    *,
    all_papers: bool = False,
    delay: float = DELAY,
    only: list[uuid.UUID] | None = None,
) -> dict[str, int]:
    query = select(Paper.id, Paper.type, Paper.source_type).where(~Paper.manual_fields.contains(["source_type"]))
    if not all_papers:
        query = query.where(Paper.source_type.is_(None))
    if only is not None:
        query = query.where(Paper.id.in_(only))
    rows = (await session.execute(query.order_by(Paper.created_at))).all()
    summary = {"checked": len(rows), "updated": 0, "asked_crossref": 0}
    for paper_id, raw_type, before in rows:
        asked = await enrichment.fill_source_type(session, paper_id, raw_type, http)
        after = await session.scalar(select(Paper.source_type).where(Paper.id == paper_id))
        summary["updated"] += after != before
        if asked:
            summary["asked_crossref"] += 1
            await asyncio.sleep(delay)  # pacing only after a request
    return summary


async def main(all_papers: bool) -> None:
    async with SessionLocal() as session:
        sources = await paper_sources.get(session)
        client = crossref.new_client(email=sources.contact_email) if sources.enabled["crossref"] else None
        try:
            summary = await run(session, client, all_papers=all_papers)
        finally:
            if client:
                await client.aclose()
    print(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="redo papers that already have a source type")
    asyncio.run(main(parser.parse_args().all))
