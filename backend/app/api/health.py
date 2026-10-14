import random

from fastapi import APIRouter
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, select, text

from app.api.deps import SessionDep
from app.config import settings
from app.models import Chunk, Paper

router = APIRouter(tags=["health"])

NEAREST_SQL = text(
    "SELECT id, embedding <=> :q AS distance FROM chunks ORDER BY embedding <=> :q LIMIT 1"
).bindparams(bindparam("q", type_=Vector(768)))


@router.get("/api/health")
async def health(session: SessionDep) -> dict:
    """Write a vector, read it back through the ORM and through raw SQL, then roll back.

    Exercises both paths the app relies on (ORM for ordinary access, text() for retrieval),
    so a broken pgvector/asyncpg type mapping shows up here instead of in retrieval.
    """
    vector = [random.random() for _ in range(768)]
    try:
        paper = Paper(title="health-check", file_path="/dev/null")
        session.add(paper)
        await session.flush()
        chunk = Chunk(
            paper_id=paper.id, ordinal=0, page=0, bbox=[[0, 0, 1, 1]], text="health",
            embedding=vector, embed_model=settings.embed_model, strategy_ver=0,
        )
        session.add(chunk)
        await session.flush()

        orm_vector = await session.scalar(select(Chunk.embedding).where(Chunk.id == chunk.id))
        nearest = (await session.execute(NEAREST_SQL, {"q": vector})).one()
    finally:
        await session.rollback()

    return {
        # pgvector stores float32, so compare with tolerance.
        "orm_roundtrip": max(abs(float(a) - b) for a, b in zip(orm_vector, vector)) < 1e-6,
        "raw_sql_nearest_is_self": nearest.id == chunk.id and nearest.distance < 1e-6,
        # Which compose project answered (D273): the E2E suite refuses any stack whose name doesn't end in -e2e.
        "stack": settings.paperlab_stack,
    }
