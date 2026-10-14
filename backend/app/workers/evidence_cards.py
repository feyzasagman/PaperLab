"""ARQ job: build evidence cards one paper at a time (M39 F5 spec §3). One job per request, on the main worker.

A big batch continues itself: past the deadline the job queues the papers it hasn't reached as a new job. The LLM
comes from ctx["llm"] when a test sets it, else the default connection's model (build_card resolves it)."""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from arq import Retry
from sqlalchemy import select, text, update

from app.core import evidence_cards, queue
from app.core.evidence_cards import JOB_TIMEOUT
from app.db import SessionLocal, engine
from app.models import EvidenceCard, workspace_papers

logger = logging.getLogger(__name__)

# JOB_TIMEOUT (core/evidence_cards.py: ~10 papers at up to 90 s each, plus slack) is the job's registered timeout;
# the loop stops itself SAFETY_MARGIN earlier and requeues what is left.
SAFETY_MARGIN = 120
RETRY_DEFER = 30  # seconds before a job that found its lock held tries again
MAX_TRIES = 60  # x RETRY_DEFER = 30 min of waiting, longer than the holder's JOB_TIMEOUT; then the cards fail "busy"
_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")
def _lock_key(workspace_id: uuid.UUID | None, paper_ids: list[uuid.UUID]) -> int:
    # Key spaces: search runs use run ids, suggestions use workspace ids ^ 1, cards use ids ^ 2. A job outside any
    # workspace locks its first paper (request_cards never queues a paper twice, so job lists don't overlap).
    return ((workspace_id or paper_ids[0]).int ^ 2) & 0x7FFFFFFFFFFFFFFF


async def _fail_interrupted(session, workspace_id: uuid.UUID | None, mine: list[uuid.UUID]) -> None:
    """Cards left `generating` by a crashed job: older than the job timeout and not in this job's list."""
    if workspace_id is None:
        return  # no workspace to scope by, and this job's own papers are the only ones it knows
    stale = (
        EvidenceCard.status == "generating",
        EvidenceCard.paper_id.not_in(mine),
        EvidenceCard.updated_at < datetime.now(UTC) - timedelta(seconds=JOB_TIMEOUT),
        EvidenceCard.paper_id.in_(
            select(workspace_papers.c.paper_id).where(workspace_papers.c.workspace_id == workspace_id)
        ),
    )
    await session.execute(update(EvidenceCard).where(*stale).values(status="failed", error="interrupted"))
    await session.commit()


async def _build_one(session, paper_id: uuid.UUID, llm) -> None:
    card = await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper_id))
    if card is None or card.status != "generating":
        return  # finished, failed or removed since it was requested
    try:
        await evidence_cards.build_card(session, paper_id, llm=llm)
    except Exception:
        logger.exception("evidence card for paper %s failed", paper_id)
        await session.rollback()
        await evidence_cards._store(
            session, evidence_cards.CardView(paper_id, "failed", "model_error")
        )


async def generate_evidence_cards(ctx: dict, workspace_id: str | None, paper_ids: list[str]) -> None:
    wid = uuid.UUID(workspace_id) if workspace_id else None
    ids = [uuid.UUID(i) for i in paper_ids]
    # Same dedicated lock connection as workers/screening_suggest.py: the session-level lock must be taken and
    # released on one physical connection however often `session` commits.
    key = _lock_key(wid, ids)
    async with engine.connect() as lock_conn:
        if not await lock_conn.scalar(_LOCK, {"key": key}):
            if ctx.get("job_try", 1) < MAX_TRIES:
                raise Retry(defer=RETRY_DEFER)  # another job is building these cards; try again shortly
            async with SessionLocal() as session:  # last try: say so instead of leaving them "generating"
                await session.execute(update(EvidenceCard).where(
                    EvidenceCard.paper_id.in_(ids), EvidenceCard.status == "generating"
                ).values(status="failed", error="busy"))
                await session.commit()
            return
        try:
            await lock_conn.commit()
            async with SessionLocal() as session:
                await _fail_interrupted(session, wid, ids)
                deadline = time.monotonic() + JOB_TIMEOUT - SAFETY_MARGIN
                for n, paper_id in enumerate(ids):
                    if time.monotonic() >= deadline:
                        # Fresh timestamps, so no other job takes the waiting cards for interrupted ones.
                        await session.execute(update(EvidenceCard).where(
                            EvidenceCard.paper_id.in_(ids[n:]), EvidenceCard.status == "generating"
                        ).values(updated_at=datetime.now(UTC)))
                        await session.commit()
                        await queue.enqueue("generate_evidence_cards", workspace_id, [str(i) for i in ids[n:]])
                        break
                    await _build_one(session, paper_id, ctx.get("llm"))
        finally:
            await lock_conn.execute(_UNLOCK, {"key": key})
            await lock_conn.commit()
