"""Starts the saved searches that are due (M39 F2, D224). Every minute and at worker start, so a time missed while
PaperLab was closed runs once when it opens."""

from datetime import datetime, timezone

from app.core import saved_searches
from app.db import SessionLocal


async def schedule_saved_searches(ctx) -> int:
    async with SessionLocal() as session:
        return len(await saved_searches.run_due(session, datetime.now(timezone.utc)))
