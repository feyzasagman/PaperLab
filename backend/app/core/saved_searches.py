"""Saved searches (M39 F2): a workspace search with a schedule. CRUD here; `run_due`/`run_now` (Task 3) start runs.
Hits go through the normal search_batch dedup, so only new ones are stored."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete as delete_rows
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import schedules, workspaces, year_range
from app.core.errors import InvalidInput, NotFound
from app.core.source_registry import DISCOVERY_SOURCE_IDS
from app.models import SavedSearch
from app.models.workspace_search import WorkspaceSearchRun

FIELDS = ("query", "sources", "year_from", "year_to", "schedule", "timezone", "enabled")


@dataclass(frozen=True)
class SavedSearchView:
    id: uuid.UUID
    workspace_id: uuid.UUID
    query: str
    sources: list[str]
    year_from: int | None
    year_to: int | None
    schedule: str
    timezone: str
    schedule_kind: str
    time: str | None
    weekday: int | None
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_run_id: uuid.UUID | None
    last_run_status: str | None
    last_error: str | None
    created_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _last_run(session: AsyncSession, saved_search_id: uuid.UUID) -> WorkspaceSearchRun | None:
    query = (
        select(WorkspaceSearchRun)
        .where(WorkspaceSearchRun.saved_search_id == saved_search_id)
        .order_by(WorkspaceSearchRun.started_at.desc())
        .limit(1)
    )
    return await session.scalar(query)


async def _view(session: AsyncSession, row: SavedSearch) -> SavedSearchView:
    kind, time, weekday = schedules.describe(row.schedule)
    last = await _last_run(session, row.id)
    return SavedSearchView(
        id=row.id,
        workspace_id=row.workspace_id,
        query=row.query,
        sources=list(row.sources),
        year_from=row.year_from,
        year_to=row.year_to,
        schedule=row.schedule,
        timezone=row.timezone,
        schedule_kind=kind,
        time=time,
        weekday=weekday,
        enabled=row.enabled,
        last_run_at=row.last_run_at,
        next_run_at=row.next_run_at,
        last_run_id=last.id if last else None,
        last_run_status=last.status if last else None,
        last_error=row.last_error,
        created_at=row.created_at,
    )


def _validate(values: dict) -> None:
    if not values["query"].strip():
        raise InvalidInput("Give the saved search a query.")
    unknown = [s for s in values["sources"] if s not in DISCOVERY_SOURCE_IDS]
    if unknown:
        raise InvalidInput("unknown_source", unknown=unknown, valid=list(DISCOVERY_SOURCE_IDS))
    year_range.parse({"year_from": values["year_from"], "year_to": values["year_to"]})


def _schedule_next(row: SavedSearch, now: datetime) -> datetime | None:
    return schedules.next_fire(row.schedule, row.timezone, now) if row.enabled else None


async def _row(session: AsyncSession, saved_search_id: uuid.UUID, workspace_id: uuid.UUID | None) -> SavedSearch:
    row = await session.get(SavedSearch, saved_search_id)
    if row is None or (workspace_id is not None and row.workspace_id != workspace_id):
        raise NotFound("saved_search_not_found", saved_search_id=str(saved_search_id))
    return row


async def create(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    query: str,
    schedule: str,
    timezone: str,
    sources=(),
    year_from: int | None = None,
    year_to: int | None = None,
    now: datetime | None = None,
) -> SavedSearchView:
    await workspaces.get(session, workspace_id)
    values = {"query": query, "sources": list(sources), "year_from": year_from, "year_to": year_to}
    _validate(values)
    row = SavedSearch(
        workspace_id=workspace_id,
        schedule=schedules.to_cron("cron", cron=schedule),
        timezone=timezone,
        enabled=True,
        **{**values, "query": query.strip()},
    )
    row.next_run_at = _schedule_next(row, now or _now())  # validates the time zone too
    session.add(row)
    await session.commit()
    return await _view(session, row)


async def update(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    saved_search_id: uuid.UUID,
    changes: dict,
    now: datetime | None = None,
) -> SavedSearchView:
    row = await _row(session, saved_search_id, workspace_id)
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise InvalidInput(f"saved searches have no {', '.join(sorted(unknown))}")
    merged = {f: changes.get(f, getattr(row, f)) for f in FIELDS}
    merged["schedule"] = schedules.to_cron("cron", cron=merged["schedule"])
    _validate(merged)
    # Validate schedule + zone even when disabled, before touching the row, so a refusal leaves it clean.
    upcoming = schedules.next_fire(merged["schedule"], merged["timezone"], now or _now())
    recompute = {"schedule", "timezone", "enabled"} & set(changes) or row.next_run_at is None
    for field, value in merged.items():
        setattr(row, field, value.strip() if field == "query" else value)
    if recompute:
        row.next_run_at = upcoming if row.enabled else None
    await session.commit()
    return await _view(session, row)


async def delete(session: AsyncSession, workspace_id: uuid.UUID, saved_search_id: uuid.UUID) -> None:
    await _row(session, saved_search_id, workspace_id)
    await session.execute(delete_rows(SavedSearch).where(SavedSearch.id == saved_search_id))
    await session.commit()


async def get(
    session: AsyncSession, saved_search_id: uuid.UUID, workspace_id: uuid.UUID | None = None
) -> SavedSearchView:
    return await _view(session, await _row(session, saved_search_id, workspace_id))


async def list_for_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> list[SavedSearchView]:
    await workspaces.get(session, workspace_id)
    rows = await session.scalars(
        select(SavedSearch).where(SavedSearch.workspace_id == workspace_id).order_by(SavedSearch.created_at.desc())
    )
    return [await _view(session, row) for row in rows]
