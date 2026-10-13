import uuid

from fastapi import APIRouter, Response

from app.api.deps import SessionDep
from app.core import saved_searches, schedules
from app.schemas.saved_searches import SavedSearchCreate, SavedSearchOut, SavedSearchUpdate
from app.schemas.workspace_search import SearchRunOut

router = APIRouter(prefix="/api/workspaces/{workspace_id}/saved-searches", tags=["saved-searches"])

SCHEDULE_FIELDS = {"schedule_kind", "time", "weekday", "cron"}


def _cron(kind: str, time: str | None, weekday: int | None, cron: str | None) -> str:
    return schedules.to_cron("cron" if kind == "custom" else kind, time, weekday, cron)


@router.get("")
async def list_saved_searches(workspace_id: uuid.UUID, session: SessionDep) -> list[SavedSearchOut]:
    return await saved_searches.list_for_workspace(session, workspace_id)


@router.post("", status_code=201)
async def create_saved_search(
    workspace_id: uuid.UUID, payload: SavedSearchCreate, session: SessionDep
) -> SavedSearchOut:
    schedule = _cron(payload.schedule_kind, payload.time, payload.weekday, payload.cron)
    return await saved_searches.create(
        session, workspace_id, query=payload.query, schedule=schedule, timezone=payload.timezone,
        sources=payload.sources, year_from=payload.year_from, year_to=payload.year_to,
    )


@router.patch("/{saved_search_id}")
async def patch_saved_search(
    workspace_id: uuid.UUID, saved_search_id: uuid.UUID, payload: SavedSearchUpdate, session: SessionDep
) -> SavedSearchOut:
    sent = payload.model_fields_set
    changes = {f: getattr(payload, f) for f in sent - SCHEDULE_FIELDS}
    if sent & SCHEDULE_FIELDS:
        current = await saved_searches.get(session, saved_search_id, workspace_id)
        kind = payload.schedule_kind or ("custom" if "cron" in sent else current.schedule_kind)
        time = payload.time if "time" in sent else current.time
        weekday = payload.weekday if "weekday" in sent else current.weekday
        changes["schedule"] = _cron(kind, time, weekday, payload.cron if "cron" in sent else current.schedule)
    return await saved_searches.update(session, workspace_id, saved_search_id, changes)


@router.delete("/{saved_search_id}", status_code=204)
async def delete_saved_search(workspace_id: uuid.UUID, saved_search_id: uuid.UUID, session: SessionDep) -> Response:
    await saved_searches.delete(session, workspace_id, saved_search_id)
    return Response(status_code=204)


@router.post("/{saved_search_id}/run", status_code=201)
async def run_saved_search(workspace_id: uuid.UUID, saved_search_id: uuid.UUID, session: SessionDep) -> SearchRunOut:
    return await saved_searches.run_now(session, workspace_id, saved_search_id)
