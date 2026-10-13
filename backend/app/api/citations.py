import uuid

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.core import pipeline
from app.core.citations_export import FORMATS, filename_slug

router = APIRouter(tags=["citations"])


class ExportIn(BaseModel):
    paper_ids: list[uuid.UUID]
    format: str  # one of FORMATS; anything else is 422 unknown_format


def download(text: str, fmt: str, name: str) -> Response:
    content_type, ext = FORMATS[fmt]
    headers = {"Content-Disposition": f'attachment; filename="{filename_slug(name)}.{ext}"'}
    return Response(text, media_type=f"{content_type}; charset=utf-8", headers=headers)


@router.post("/api/citations/export")
async def export_selection(payload: ExportIn, session: SessionDep) -> Response:
    """The selected papers, in the order given, as a download. 422 for an unknown format, none, or over 1000."""
    text, _ = await pipeline.export_citations(session, paper_ids=payload.paper_ids, fmt=payload.format)
    return download(text, payload.format, "citations")
