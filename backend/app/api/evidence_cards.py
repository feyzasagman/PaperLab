import uuid

from fastapi import APIRouter, Response

from app.api.deps import SessionDep
from app.core import evidence_cards
from app.schemas.evidence_cards import CardOut, CardRowOut, EditIn, GenerateIn

router = APIRouter(tags=["evidence-cards"])


@router.get("/api/papers/{paper_id}/evidence-card")
async def get_card(paper_id: uuid.UUID, session: SessionDep) -> CardOut:
    return await evidence_cards.get_card(session, paper_id)


@router.post("/api/papers/{paper_id}/evidence-card", status_code=202)
async def generate_card(paper_id: uuid.UUID, session: SessionDep, payload: GenerateIn | None = None) -> list[CardOut]:
    payload = payload or GenerateIn()
    return await evidence_cards.request_cards(
        session, paper_ids=[paper_id], regenerate=payload.regenerate, confirm=payload.confirm
    )


@router.patch("/api/papers/{paper_id}/evidence-card")
async def edit_card(paper_id: uuid.UUID, payload: EditIn, session: SessionDep) -> CardOut:
    return await evidence_cards.edit_card(session, paper_id, payload.changes)


@router.get("/api/workspaces/{workspace_id}/evidence-cards")
async def list_cards(workspace_id: uuid.UUID, session: SessionDep) -> list[CardRowOut]:
    return await evidence_cards.workspace_cards(session, workspace_id)


@router.post("/api/workspaces/{workspace_id}/evidence-cards", status_code=202)
async def generate_cards(
    workspace_id: uuid.UUID, session: SessionDep, payload: GenerateIn | None = None
) -> list[CardOut]:
    payload = payload or GenerateIn()
    rows = await evidence_cards.workspace_cards(session, workspace_id)
    return await evidence_cards.request_cards(
        session, paper_ids=[r.paper_id for r in rows], workspace_id=workspace_id,
        regenerate=payload.regenerate, confirm=payload.confirm,
    )


@router.get("/api/workspaces/{workspace_id}/evidence-cards.csv")
async def cards_csv(workspace_id: uuid.UUID, session: SessionDep) -> Response:
    text = await evidence_cards.workspace_csv(session, workspace_id)
    return Response(
        text.encode("utf-8"), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="evidence-cards.csv"'},
    )
