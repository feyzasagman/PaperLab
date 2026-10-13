import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, UploadFile

from app.api.deps import DiscoveryDep, SessionDep
from app.core import screening, workspace_search
from app.core.errors import InvalidInput
from app.core.prisma_export import prisma_export
from app.core.queue import INGEST_QUEUE
from app.core.screening import RANKED_LIMIT_MAX
from app.schemas.workspace_search import (
    AcquisitionStatus,
    BulkHitReviewUpdate,
    BulkUpdateOut,
    ClearHitsOut,
    HitListOut,
    HitOut,
    HitReviewUpdate,
    ImportHitsOut,
    ImportHitsRequest,
    PrismaExportOut,
    RankedHitsOut,
    ScreeningCriteriaUpdate,
    ScreeningStateOut,
    SearchRunCreate,
    SearchRunOut,
    SnowballOut,
    SnowballRequest,
    Stage1Status,
    SuggestStart,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}/search", tags=["workspace-search"])


async def _enqueue(request: Request, run_id: uuid.UUID) -> None:
    await request.app.state.arq.enqueue_job("run_workspace_search", str(run_id))


async def _enqueue_ingest(request: Request, paper_id: uuid.UUID) -> None:
    await request.app.state.arq.enqueue_job("ingest_paper", str(paper_id), _queue_name=INGEST_QUEUE)


@router.post("/runs", status_code=201)
async def start_run(
    workspace_id: uuid.UUID, payload: SearchRunCreate, session: SessionDep, request: Request
) -> SearchRunOut:
    run = await workspace_search.start_run(
        session, workspace_id, payload.query, payload.filters, payload.sources, payload.query_overrides,
    )
    await _enqueue(request, run.id)
    return run


@router.post("/runs/{run_id}")
async def restart_run(
    workspace_id: uuid.UUID, run_id: uuid.UUID, session: SessionDep, request: Request
) -> SearchRunOut:
    run = await workspace_search.get_run(session, run_id)
    resumed = await workspace_search.start_run(
        session, workspace_id, run.query_text, run.filters_json, run.sources_json,
        run.query_overrides_json, run_id=run_id,
    )
    await _enqueue(request, run_id)
    return resumed


@router.post("/runs/{run_id}/stop")
async def stop_run(workspace_id: uuid.UUID, run_id: uuid.UUID, session: SessionDep) -> SearchRunOut:
    return await workspace_search.stop_run(session, run_id, workspace_id=workspace_id)


@router.get("/runs/{run_id}")
async def get_run(workspace_id: uuid.UUID, run_id: uuid.UUID, session: SessionDep) -> SearchRunOut:
    return await workspace_search.get_run(session, run_id, workspace_id=workspace_id)


@router.get("/hits")
async def list_hits(
    workspace_id: uuid.UUID, session: SessionDep, limit: Annotated[int, Query(gt=0)] = 50,
    after: str | None = None, stage1_status: Stage1Status | None = None,
    acquisition_status: AcquisitionStatus | None = None,
) -> HitListOut:
    items, next_cursor = await workspace_search.list_hits(
        session, workspace_id, limit, after, stage1_status, acquisition_status
    )
    return HitListOut(items=items, next_cursor=next_cursor)


@router.get("/hits/ranked")
async def ranked_hits(
    workspace_id: uuid.UUID, session: SessionDep, limit: Annotated[int, Query(gt=0, le=RANKED_LIMIT_MAX)] = 200,
) -> RankedHitsOut:
    return await screening.ranked_hits(session, workspace_id, limit)


@router.get("/screening")
async def get_screening(workspace_id: uuid.UUID, session: SessionDep) -> ScreeningStateOut:
    return await screening.get_state(session, workspace_id)


@router.put("/screening")
async def put_screening(
    workspace_id: uuid.UUID, payload: ScreeningCriteriaUpdate, session: SessionDep
) -> ScreeningStateOut:
    return await screening.set_criteria(session, workspace_id, payload.criteria)


@router.post("/suggestions")
async def start_suggestions(
    workspace_id: uuid.UUID, payload: SuggestStart, request: Request, session: SessionDep
) -> ScreeningStateOut:
    state = await screening.start_suggestions(session, workspace_id, payload.confirm_remote)
    await request.app.state.arq.enqueue_job("suggest_screening", str(workspace_id))
    return state


@router.post("/suggestions/stop")
async def stop_suggestions(workspace_id: uuid.UUID, session: SessionDep) -> ScreeningStateOut:
    return await screening.stop_suggestions(session, workspace_id)


@router.delete("/hits")
async def clear_hits(workspace_id: uuid.UUID, session: SessionDep) -> ClearHitsOut:
    deleted = await workspace_search.clear_hits(session, workspace_id)
    return ClearHitsOut(deleted=deleted)


# Registered before "/hits/{hit_id}": both are PATCH routes and Starlette matches path templates in registration
# order, so "/hits/bulk" must come first or a PATCH to it would be swallowed by "/hits/{hit_id}" (and fail there
# trying to parse "bulk" as a UUID).
@router.patch("/hits/bulk")
async def patch_hits_bulk(workspace_id: uuid.UUID, payload: BulkHitReviewUpdate, session: SessionDep) -> BulkUpdateOut:
    count = await workspace_search.bulk_review_hits(
        session, workspace_id, payload.hit_ids, payload.stage1_status, payload.stage1_exclude_reason, payload.priority,
    )
    return BulkUpdateOut(updated=count)


@router.patch("/hits/{hit_id}")
async def patch_hit(
    workspace_id: uuid.UUID, hit_id: uuid.UUID, payload: HitReviewUpdate, session: SessionDep
) -> HitOut:
    return await workspace_search.review_hit(session, hit_id, workspace_id, payload)


@router.post("/hits/import")
async def import_hits(
    workspace_id: uuid.UUID, payload: ImportHitsRequest, session: SessionDep, providers: DiscoveryDep, request: Request
) -> ImportHitsOut:
    result = await workspace_search.import_hits(session, providers, workspace_id, payload.hit_ids)
    for paper_id in result.paper_ids:
        await _enqueue_ingest(request, paper_id)
    return ImportHitsOut(imported=result.imported, failed=result.failed)


@router.post("/hits/{hit_id}/upload")
async def upload_hit_pdf(
    workspace_id: uuid.UUID, hit_id: uuid.UUID, session: SessionDep, request: Request,
    file: UploadFile = File(...),
) -> HitOut:
    content = await file.read()
    hit, created = await workspace_search.upload_hit_pdf(session, workspace_id, hit_id, file.filename, content)
    if created:
        await _enqueue_ingest(request, hit["paper_id"])
    return hit


@router.post("/snowball")
async def snowball_route(
    workspace_id: uuid.UUID, payload: SnowballRequest, session: SessionDep, providers: DiscoveryDep,
) -> SnowballOut:
    result = await workspace_search.snowball(
        session, providers, workspace_id, payload.seed_paper_ids, payload.seed_hit_ids,
        payload.backward, payload.forward,
    )
    return SnowballOut(new_hits=result.new_hits, skipped_seeds=result.skipped_seeds, errors=result.errors)


@router.get("/prisma")
async def prisma_export_route(workspace_id: uuid.UUID, session: SessionDep, runs: str = "all") -> PrismaExportOut:
    run_id: uuid.UUID | None = None
    if runs != "all":
        try:
            run_id = uuid.UUID(runs)
        except ValueError as exc:
            raise InvalidInput(f"invalid runs value: {runs!r}") from exc
    export = await prisma_export(session, workspace_id, run_id)
    return export
