"""Screening assist for M30's stage 1 (M31): a ranking that learns from the reader's decisions, and (M31b) the
state of the local-model suggestion job. Suggest only: nothing here ever writes stage1_status."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import llm_connections
from app.core.errors import Conflict, NotFound
from app.core.screening_rank import Doc, rank, stop_hint
from app.core.workspace_search import _hit_out_dict
from app.models.references import ExternalRef
from app.models.workspace import Workspace
from app.models.workspace_search import WorkspaceSearchHit, WorkspaceSearchRun
from app.providers.llm import ANTHROPIC_HOST, host_of

RANKED_LIMIT_MAX = 500
# D265: auto pre-screen after a scheduled run scores this many of the run's new hits, best (ranked) first; the rest
# stay unscored and the owner can press Suggest for them (manual runs are not capped).
AUTO_PRESCREEN_MAX = 200
_POSITIVE = {"relevant", "maybe"}


@dataclass(frozen=True)
class Pool:
    workspace: Workspace
    rows: list[tuple[WorkspaceSearchHit, ExternalRef | None]]  # found order: (first_seen_at, id)


async def load_pool(session: AsyncSession, workspace_id: uuid.UUID) -> Pool:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound(f"workspace {workspace_id} not found")
    query = (
        select(WorkspaceSearchHit, ExternalRef)
        .join(ExternalRef, WorkspaceSearchHit.external_ref_id == ExternalRef.id, isouter=True)
        .where(WorkspaceSearchHit.workspace_id == workspace_id)
        .order_by(WorkspaceSearchHit.first_seen_at, WorkspaceSearchHit.id)
    )
    return Pool(workspace, [(hit, ref) for hit, ref in (await session.execute(query)).all()])


def hit_text(hit: WorkspaceSearchHit, ref: ExternalRef | None) -> str:
    """Title and abstract; a hit with no linked ref still has its normalized title."""
    if ref is None:
        return hit.normalized_title
    return f"{ref.title} {ref.abstract or ''}"


def _label(status: str | None) -> bool | None:
    return None if status is None else status in _POSITIVE


def ranked_order(pool: Pool) -> tuple[list[uuid.UUID], bool]:
    return rank([Doc(hit.id, hit_text(hit, ref), _label(hit.stage1_status)) for hit, ref in pool.rows])


async def ranked_hits(session: AsyncSession, workspace_id: uuid.UUID, limit: int) -> dict:
    pool = await load_pool(session, workspace_id)
    order, trained = ranked_order(pool)
    by_id = {hit.id: (hit, ref) for hit, ref in pool.rows}
    decided = sorted(
        (hit for hit, _ in pool.rows if hit.stage1_decided_at is not None and hit.stage1_status is not None),
        key=lambda hit: hit.stage1_decided_at,
        reverse=True,
    )
    hint = stop_hint([hit.stage1_status for hit in decided], len(pool.rows), trained)
    if trained and not pool.workspace.screening_ranked_used:
        pool.workspace.screening_ranked_used = True  # PRISMA reports the automation (spec §3.5)
        await session.commit()
    return {
        "items": [_hit_out_dict(*by_id[hit_id]) for hit_id in order[:limit]],
        "trained": trained,
        "total_unscreened": len(order),
        "streak": hint.streak,
        "threshold": hint.threshold,
        "show_stop_hint": hint.show,
    }


CRITERIA_MAX_CHARS = 4000


async def _default_model(session: AsyncSession) -> tuple[str, bool, str] | None:
    """(label, is_local, host) of the default chat model, or None when there is none (spec §4.3)."""
    try:
        connection, model = await llm_connections.resolve(session, None)
    except Conflict:
        return None
    return (
        f"{connection.label} · {model.name}",
        llm_connections.is_local(connection.kind, connection.base_url),
        host_of(connection.base_url) if connection.base_url else ANTHROPIC_HOST,
    )


async def get_state(session: AsyncSession, workspace_id: uuid.UUID) -> dict:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound(f"workspace {workspace_id} not found")
    model = await _default_model(session)
    capped_stats = await session.scalar(
        select(WorkspaceSearchRun.stats_json["prescreen_capped"]).where(
            WorkspaceSearchRun.workspace_id == workspace_id, WorkspaceSearchRun.stats_json.has_key("prescreen_capped")
        ).order_by(WorkspaceSearchRun.started_at.desc()).limit(1)
    )
    return {
        "capped": f"{capped_stats['queued']} of {capped_stats['total']}" if capped_stats else None,
        "capped_left": capped_stats["total"] - capped_stats["queued"] if capped_stats else None,
        "criteria": workspace.inclusion_criteria,
        "research_question": workspace.research_question,
        "inclusion_criteria": workspace.inclusion_criteria,
        "exclusion_criteria": workspace.exclusion_criteria,
        "prescreen_after_scheduled_runs": workspace.prescreen_after_scheduled_runs,
        "auto_import_min_relevance": workspace.auto_import_min_relevance,
        "suggest_run_id": workspace.suggest_run_id,
        "ranked_used": workspace.screening_ranked_used,
        "suggest_status": workspace.suggest_status,
        "suggest_done": workspace.suggest_done,
        "suggest_total": workspace.suggest_total,
        "suggest_error": workspace.suggest_error,
        "model_label": model[0] if model else None,
        "model_is_local": model[1] if model else None,
        "model_host": model[2] if model else None,
    }


_UNSET = object()  # "leave this field as it is"; None or blank clears it
_SUGGESTION_COLUMNS = dict.fromkeys(
    ("suggestion", "suggestion_reason", "suggestion_note", "suggestion_model", "suggested_at", "suggestion_relevance",
     "suggestion_quotes", "suggestion_confidence", "suggestion_flags", "suggestion_prompt_version")
)


def _criteria_summary(workspace: Workspace) -> str | None:
    parts = (("Research question", workspace.research_question), ("Include when", workspace.inclusion_criteria),
             ("Exclude when", workspace.exclusion_criteria))
    return "\n".join(f"{label}: {text}" for label, text in parts if text) or None


async def set_criteria(
    session: AsyncSession, workspace_id: uuid.UUID, criteria=_UNSET, *,
    research_question=_UNSET, inclusion=_UNSET, exclusion=_UNSET,
) -> dict:
    """Saves the question and criteria; a change to any clears every stored suggestion, judged against the old
    ones (§4.1). The old `criteria` is the inclusion criteria."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound(f"workspace {workspace_id} not found")
    if workspace.suggest_status != "idle":
        raise Conflict("Stop the suggestions before changing the criteria")
    if inclusion is _UNSET:
        inclusion = criteria
    changed = False
    for column, value in (("research_question", research_question), ("inclusion_criteria", inclusion),
                          ("exclusion_criteria", exclusion)):
        if value is _UNSET:
            continue
        value = (value or "").strip() or None
        if value != getattr(workspace, column):
            setattr(workspace, column, value)
            changed = True
    if changed:
        workspace.screening_criteria = _criteria_summary(workspace)  # the PRISMA export reads this column
        await session.execute(
            update(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == workspace_id)
            .values(**_SUGGESTION_COLUMNS)
        )
    await session.commit()
    return await get_state(session, workspace_id)


async def set_prescreen_settings(
    session: AsyncSession, workspace_id: uuid.UUID, *, after_scheduled_runs: bool | None,
    auto_import_min_relevance: int | None,
) -> dict:
    """Auto-run after scheduled runs (None = leave) and the auto-import bar (None = off; else 4 or 5)."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound(f"workspace {workspace_id} not found")
    if auto_import_min_relevance not in (None, 4, 5):
        raise Conflict("Auto-import needs a relevance of 4 or 5")
    if after_scheduled_runs is not None:
        workspace.prescreen_after_scheduled_runs = after_scheduled_runs
    workspace.auto_import_min_relevance = auto_import_min_relevance
    await session.commit()
    return await get_state(session, workspace_id)


def suggestion_queue(pool: Pool, run_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Unscreened hits still without a suggestion and with some text to judge, in ranked order (§4.2); with a
    run_id, only that run's hits."""
    order, _ = ranked_order(pool)
    by_id = {hit.id: (hit, ref) for hit, ref in pool.rows}
    return [
        hit_id for hit_id in order
        if by_id[hit_id][0].suggestion is None and hit_text(*by_id[hit_id]).strip()
        and (run_id is None or by_id[hit_id][0].run_id == run_id)
    ]


async def start_suggestions(
    session: AsyncSession, workspace_id: uuid.UUID, confirm_remote: bool, run_id: uuid.UUID | None = None,
    limit: int | None = None,
) -> dict:
    """Marks the job running; the route enqueues it. Suggest only (D189): the job never writes stage1_status."""
    pool = await load_pool(session, workspace_id)
    workspace = pool.workspace
    if not (workspace.research_question or workspace.inclusion_criteria):
        raise Conflict("no_criteria")
    if workspace.suggest_status != "idle":
        raise Conflict("Suggestions are already running")
    model = await _default_model(session)
    if model is None:
        raise Conflict("no_model")
    label, local, host = model
    if not local and not confirm_remote:
        raise Conflict(f"Abstracts will be sent to {host} and may cost money")
    workspace.suggest_status = "running"
    workspace.suggest_done = 0
    queued = len(suggestion_queue(pool, run_id))
    workspace.suggest_total = queued if limit is None else min(queued, limit)  # the job takes this many, best first
    if run_id is not None and limit is not None and queued > limit:
        run = await session.get(WorkspaceSearchRun, run_id)
        run.stats_json = {**run.stats_json, "prescreen_capped": {"queued": limit, "total": queued}}
    workspace.suggest_run_id = run_id
    workspace.suggest_error = None
    await session.commit()
    return await get_state(session, workspace_id)


async def stop_suggestions(session: AsyncSession, workspace_id: uuid.UUID) -> dict:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFound(f"workspace {workspace_id} not found")
    if workspace.suggest_status == "running":
        workspace.suggest_status = "stopping"  # the job reads this before each hit
        await session.commit()
    elif workspace.suggest_status == "stopping":
        # A second Stop click on an already-stopping job: either it's about to finish on its own (harmless,
        # redundant) or it's stuck (lost worker, no job left to ever make the transition) — found live on
        # 2026-10-03, where the only way back was a manual database fix. The advisory lock, not this column, is
        # what actually keeps two jobs from working a workspace at once, so forcing this to idle is safe either way.
        workspace.suggest_status = "idle"
        await session.commit()
    return await get_state(session, workspace_id)


@dataclass(frozen=True)
class AcceptResult:
    accepted: int
    skipped: list[uuid.UUID]


async def accept_suggestions(session: AsyncSession, workspace_id: uuid.UUID, hit_ids: list[uuid.UUID]) -> AcceptResult:
    """The owner's Accept: an include becomes relevant, an exclude not_relevant with its reason, stamped like a manual
    decision. Unsure, unsuggested, already decided, foreign and unknown hits are skipped, never written."""
    hits = (await session.scalars(
        select(WorkspaceSearchHit).where(
            WorkspaceSearchHit.id.in_(hit_ids), WorkspaceSearchHit.workspace_id == workspace_id
        )
    )).all()
    now = datetime.now(timezone.utc)
    accepted = set()
    for hit in hits:
        if hit.stage1_status is not None:
            continue
        if hit.suggestion == "include":
            hit.stage1_status = "relevant"
        elif hit.suggestion == "exclude" and hit.suggestion_reason:
            hit.stage1_status, hit.stage1_exclude_reason = "not_relevant", hit.suggestion_reason
        else:
            continue
        hit.stage1_decided_at = now
        accepted.add(hit.id)
    await session.commit()
    return AcceptResult(len(accepted), [i for i in dict.fromkeys(hit_ids) if i not in accepted])


async def prescreen_after_run(session: AsyncSession, run: WorkspaceSearchRun) -> str | None:
    """Starts pre-screening for a scheduled run that just finished (spec §4). None when started (the worker then
    enqueues the job), else why not. A remote default model is the one case the owner is told about."""
    if run.saved_search_id is None or run.status != "exhausted":
        return "not a finished scheduled run"
    workspace = await session.get(Workspace, run.workspace_id)
    if not workspace.prescreen_after_scheduled_runs:
        return "pre-screening after scheduled runs is off"
    if not (workspace.research_question or workspace.inclusion_criteria):
        workspace.suggest_error = (
            "New hits from a scheduled search weren't pre-screened: "
            "add a research question or inclusion criteria first."
        )
        await session.commit()
        return "no criteria"
    if workspace.suggest_status != "idle":
        workspace.suggest_error = (
            "New hits from a scheduled search weren't pre-screened because another job was running. "
            "Press Suggest to score them."
        )
        await session.commit()
        return "suggestions already running"
    model = await _default_model(session)
    if model is None:
        return "no model"
    if not model[1]:
        why = (f"Pre-screening did not start after the scheduled run: abstracts would go to {model[2]}. "
               "Start it yourself to confirm.")
        workspace.suggest_error = why
        await session.commit()
        return why
    await start_suggestions(session, workspace.id, False, run.id, limit=AUTO_PRESCREEN_MAX)
    return None
