"""ARQ job: one local-model suggestion per unscreened hit, in ranked order (M31b spec §4.2). Suggest only: it writes
the suggestion columns and the workspace's job state, never stage1_status."""

import logging
import time
import uuid
from collections.abc import Collection
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.core import discovery, llm_connections, queue, workspace_search
from app.core.errors import Conflict
from app.core.screening import hit_text, load_pool, suggestion_queue
from app.core.screening_suggest import SCREENING_PROMPT_VERSION, SuggestionV2, suggest
from app.db import SessionLocal, engine
from app.models.references import ExternalRef
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchHit
from app.providers.base import LLMError, LLMUnavailable
from app.providers.llm import build_llm

logger = logging.getLogger(__name__)

# ~3,000 hits at 1-3 s each on a Mac. Registered as the job's timeout in workers/settings.py; the loop stops itself
# SAFETY_MARGIN earlier, and the button resumes with the hits still left.
SUGGEST_JOB_TIMEOUT = 4 * 3600
SAFETY_MARGIN = 60
_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")


def _lock_key(workspace_id: uuid.UUID) -> int:
    # A different key space from the search runs' locks: their keys are run ids, these are workspace ids ^ 1.
    return (workspace_id.int ^ 1) & 0x7FFFFFFFFFFFFFFF


async def _suggest_one(llm, workspace: Workspace, hit: WorkspaceSearchHit, ref: ExternalRef | None) -> SuggestionV2:
    title = ref.title if ref else hit.normalized_title
    try:
        return await suggest(
            llm, workspace.research_question, workspace.inclusion_criteria, workspace.exclusion_criteria,
            title, ref.abstract if ref else None,
        )
    except LLMUnavailable:
        raise
    except LLMError:
        return SuggestionV2("unsure", None, "model error", flags=["model_error"])


async def _auto_import(session, workspace: Workspace, run_ids: Collection[uuid.UUID] | None) -> None:
    """Imports the free-PDF hits rated at or above the owner's bar, and queues their ingest (D232). Never decides."""
    query = select(WorkspaceSearchHit.id).where(
        WorkspaceSearchHit.workspace_id == workspace.id,
        WorkspaceSearchHit.suggestion == "include",
        WorkspaceSearchHit.suggestion_relevance >= workspace.auto_import_min_relevance,
        WorkspaceSearchHit.stage1_status.is_(None),
        WorkspaceSearchHit.acquisition_status == "not_attempted",
    )
    if run_ids is not None:
        query = query.where(WorkspaceSearchHit.run_id.in_(run_ids))
    ids = list((await session.execute(query)).scalars())
    if not ids:
        return
    async with discovery.open_providers(session) as providers:
        result = await workspace_search.import_hits(session, providers, workspace.id, ids)
    for paper_id in result.paper_ids:
        await queue.enqueue("ingest_paper", str(paper_id))


async def suggest_screening(ctx: dict, workspace_id: str, run_ids: list[str] | None = None) -> None:
    wid = uuid.UUID(workspace_id)
    # The advisory lock is session-level: Postgres ties it to one physical connection, not to our ORM `session`
    # object. But `session`'s own connection can rotate to a *different* pooled connection on every
    # `session.commit()` (the engine checks a connection back out for the next statement, which under real
    # concurrent load — e.g. the frontend polling GET /screening while this job runs — need not be the same one).
    # Taking and releasing the lock through `session` let the lock-holding connection drift away mid-job, leaving
    # the original connection's lock stuck forever once it went idle in the pool — found live on 2026-10-03, a
    # stuck "stopping" job blocking every future suggestion run on the workspace. One dedicated connection, held
    # open for the whole job, keeps lock and unlock on the same physical session no matter how often `session`
    # commits.
    async with engine.connect() as lock_conn:
        async with SessionLocal() as session:
            workspace = await session.get(Workspace, wid)
            if workspace is None or workspace.suggest_status != "running":
                return
            if not await lock_conn.scalar(_LOCK, {"key": _lock_key(wid)}):
                logger.info("suggestions for workspace %s already running elsewhere", wid)
                return
            try:
                # End lock_conn's own implicit transaction now, inside the try so a failure here still reaches
                # the finally's unlock below: the advisory lock itself isn't transactional (it survives
                # commit/rollback either way), but leaving this open would sit "idle in transaction" for up to
                # SUGGEST_JOB_TIMEOUT, holding back autovacuum the whole time.
                await lock_conn.commit()
                connection, model = await llm_connections.resolve(session, None)
                llm = build_llm(connection, model.name, transport=ctx.get("transport"))
                label = f"{connection.label} · {model.name}"
                pool = await load_pool(session, wid)
                rows = {hit.id: (hit, ref) for hit, ref in pool.rows}
                # A multi-run job names its runs; otherwise the single run (or none) the start recorded.
                scope = (
                    {uuid.UUID(r) for r in run_ids} if run_ids is not None
                    else None if workspace.suggest_run_id is None else {workspace.suggest_run_id}
                )
                deadline = time.monotonic() + SUGGEST_JOB_TIMEOUT - SAFETY_MARGIN
                hit_deadline = False
                queue_ids = suggestion_queue(pool, run_ids=scope)[: workspace.suggest_total or None]  # D265: the cap
                for hit_id in queue_ids:
                    await session.refresh(workspace)
                    if workspace.suggest_status != "running":
                        break
                    if time.monotonic() >= deadline:
                        hit_deadline = True
                        break
                    hit, ref = rows[hit_id]
                    await session.refresh(hit)
                    if hit.stage1_status is not None or hit.suggestion is not None or not hit_text(hit, ref).strip():
                        continue  # decided (or suggested) since the job started
                    suggestion = await _suggest_one(llm, workspace, hit, ref)
                    hit.suggestion, hit.suggestion_reason = suggestion.verdict, suggestion.reason
                    hit.suggestion_note = suggestion.note
                    # No abstract means no model was called, so there is no model to name.
                    hit.suggestion_model = None if "no_abstract" in suggestion.flags else label
                    hit.suggestion_relevance, hit.suggestion_quotes = suggestion.relevance, suggestion.quotes
                    hit.suggestion_confidence, hit.suggestion_flags = suggestion.confidence, suggestion.flags
                    hit.suggestion_prompt_version = SCREENING_PROMPT_VERSION
                    hit.suggested_at = datetime.now(timezone.utc)
                    workspace.suggest_done += 1
                    await session.commit()
                # Only a normal finish imports: not a Stop (pressed during the last hit too) or the deadline.
                await session.refresh(workspace)
                if hit_deadline and workspace.suggest_status == "running":
                    workspace.suggest_error = (
                        f"Stopped at the {SUGGEST_JOB_TIMEOUT // 3600}-hour limit after {workspace.suggest_done} of "
                        f"{workspace.suggest_total}; press Suggest to continue with the rest."
                    )
                if (
                    workspace.suggest_status == "running" and time.monotonic() < deadline
                    and workspace.auto_import_min_relevance is not None
                ):
                    await _auto_import(session, workspace, scope)
            except (LLMUnavailable, Conflict) as exc:
                workspace.suggest_error = (
                    "Set up a chat model in Settings first" if isinstance(exc, Conflict) else str(exc)
                )
            except Exception:
                logger.exception("screening suggestions for workspace %s failed", wid)
                await session.rollback()  # a failed flush leaves the session unusable until rolled back
                workspace.suggest_error = "Suggestions stopped after an unexpected error"
            finally:
                # The unlock must run even if this commit fails or is cancelled.
                workspace.suggest_status = "idle"
                workspace.suggest_run_id = None
                try:
                    await session.commit()
                finally:
                    await lock_conn.execute(_UNLOCK, {"key": _lock_key(wid)})
                    await lock_conn.commit()
