"""What Claude does over MCP (M39 F1): each function backs one tool, composing existing core services.

Workspaces come by name (D93). Jobs go through `queue`, paper sources through `discovery.open_providers`. No SQL here:
every read and write is an existing service's. Results are the API's own schemas, so Claude and the app see the same
fields.
"""

import logging
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import (
    citations_export,
    claims,
    discovery,
    evidence_cards,
    hit_enrichment,
    notes,
    paper_enrichment,
    paper_sources,
    papers,
    pdf_links,
    prisma_export,
    queue,
    saved_searches,
    schedules,
    screening,
    tags,
    workspace_search,
    workspaces,
)
from app.core.errors import Conflict, DomainError, InvalidInput, NotFound, error_code
from app.core.source_registry import DISCOVERY_SOURCE_IDS
from app.models import PaperStatus
from app.providers.base import LLMError, LLMUnavailable
from app.schemas.papers import PaperOut
from app.schemas.workspace_search import HitOut, ScreeningStateOut, SearchRunOut, SearchRunSummary

workspace_id = workspaces.by_name


@dataclass(frozen=True)
class Removed:
    workspace: str
    paper_id: uuid.UUID
    removed: bool = True


async def list_papers(
    session: AsyncSession,
    workspace: str,
    tags_: list[str] | None = None,
    match: Literal["any", "all"] = "any",
    full_text: bool | None = None,
) -> list[PaperOut]:
    ws = await workspace_id(session, workspace)
    rows = await workspaces.papers(session, ws)
    wanted = await tags.paper_ids(session, ws, tags_, match)
    return [
        PaperOut.model_validate(row)
        for row in rows
        if (wanted is None or row.id in wanted)
        and (full_text is None or (row.evidence_access == "full_text") == full_text)
    ]


async def list_tags(session: AsyncSession, workspace: str) -> list[tags.TagSummary]:
    return await tags.summaries(session, await workspace_id(session, workspace))


async def tag_papers(
    session: AsyncSession, workspace: str, tag: str, paper_ids: list[uuid.UUID] | None, dois: list[str] | None
) -> tags.TagResult:
    return await tags.tag_papers(session, await workspace_id(session, workspace), tag, paper_ids or (), dois or ())


async def untag_papers(
    session: AsyncSession, workspace: str, tag: str, paper_ids: list[uuid.UUID] | None, dois: list[str] | None
) -> tags.TagResult:
    return await tags.untag_papers(session, await workspace_id(session, workspace), tag, paper_ids or (), dois or ())


@dataclass(frozen=True)
class TagRenamed:
    old: str
    new: str


@dataclass(frozen=True)
class TagDeleted:
    tag: str
    papers: int


async def rename_tag(session: AsyncSession, workspace: str, tag: str, new_name: str) -> TagRenamed:
    ws = await workspace_id(session, workspace)
    (found,) = await tags.resolve(session, ws, [tag])
    old = found.name
    return TagRenamed(old, await tags.rename(session, ws, tag, new_name))


async def delete_tag(session: AsyncSession, workspace: str, tag: str) -> TagDeleted:
    ws = await workspace_id(session, workspace)
    (found,) = await tags.resolve(session, ws, [tag])
    name = found.name
    return TagDeleted(name, await tags.delete(session, ws, tag))


async def add_paper_to_workspace(session: AsyncSession, workspace: str, paper_id: uuid.UUID) -> PaperOut:
    await workspaces.add_paper(session, await workspace_id(session, workspace), paper_id)
    return PaperOut.model_validate(await papers.get_paper(session, paper_id))


async def remove_paper_from_workspace(session: AsyncSession, workspace: str, paper_id: uuid.UUID) -> Removed:
    await workspaces.remove_paper(session, await workspace_id(session, workspace), paper_id)
    return Removed(workspace=workspace.strip(), paper_id=paper_id)


@dataclass(frozen=True)
class AddPaperResult:
    status: Literal["queued", "ingested", "no_free_pdf"]
    paper_id: uuid.UUID | None = None
    landing_url: str | None = None
    title: str | None = None


DOI_ARXIV_HOSTS = ("doi.org", "dx.doi.org", "arxiv.org", "www.arxiv.org")


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:  # malformed: check_url refuses it as unsafe_url
        return ""


def _identifier(identifier: str) -> tuple[str, str]:
    """("doi" | "arxiv" | "link", value). A title or an OpenAlex ID is refused (D217)."""
    text = identifier.strip().removeprefix("doi:").removeprefix("DOI:").strip()
    if "://" in text and _host(text) not in DOI_ARXIV_HOSTS:
        return "link", text  # a publisher PDF link can carry a DOI in its path
    kind, value = discovery.classify_query(text)
    if kind in ("doi", "arxiv"):
        return kind, value
    if "://" in text:  # a title with a colon parses as a URL scheme, so only "://" marks a link
        return "link", text
    raise InvalidInput("invalid_identifier", hint="Use a DOI, an arXiv ID or a direct PDF link.")


def _landing_url(kind: str, value: str) -> str:
    return f"https://doi.org/{value}" if kind == "doi" else f"https://arxiv.org/abs/{value}"


async def _queued(session: AsyncSession, paper, workspace_uuid: uuid.UUID | None) -> AddPaperResult:
    if workspace_uuid is not None:
        await workspaces.add_paper(session, workspace_uuid, paper.id)
    await queue.enqueue("ingest_paper", str(paper.id))
    return AddPaperResult(status="queued", paper_id=paper.id, title=paper.title)


async def _add_found(session, providers, kind, value, workspace_uuid) -> AddPaperResult:
    found = await discovery.search(session, providers, value)
    match = next(
        (c for c in found.results if (c.doi or "").lower() == value or c.external_ids.get("arxiv") == value), None
    )
    if match is None:
        raise NotFound("no_matching_paper", identifier=value)
    if match.paper_id is not None:
        paper = await papers.get_paper(session, match.paper_id)
        if workspace_uuid is not None:
            await workspaces.add_paper(session, workspace_uuid, paper.id)
        if paper.status == PaperStatus.FAILED:  # a retry, as the app's reingest does
            await papers.set_status(session, paper.id, PaperStatus.UPLOADED)
            await queue.enqueue("ingest_paper", str(paper.id))
        status = "ingested" if paper.status == PaperStatus.READY else "queued"
        return AddPaperResult(status=status, paper_id=paper.id, title=paper.title)
    try:
        paper = await discovery.add(session, providers, match, settings.pdf_dir)
    except Conflict as exc:
        if str(exc) != discovery.NO_FREE_PDF:
            raise
        return AddPaperResult(status="no_free_pdf", landing_url=_landing_url(kind, value), title=match.title)
    return await _queued(session, paper, workspace_uuid)


async def _add_link(session, providers, link, workspace_uuid) -> AddPaperResult:
    await pdf_links.check_url(link)
    if pdf_links.guard not in providers.pdf.event_hooks["request"]:
        providers.pdf.event_hooks["request"].append(pdf_links.guard)  # this call's own client, closed after it
    data = await discovery.download_pdf(providers.pdf, [link])
    if data is None:
        return AddPaperResult(status="no_free_pdf", landing_url=link)
    # Like an upload: extraction and enrichment find the metadata. A paper already in the library becomes a second
    # copy, exactly as uploading the same PDF twice does (enrichment drops a DOI another paper holds).
    name = PurePosixPath(urlsplit(link).path).name or "paper.pdf"
    paper = await papers.create_paper(session, name, data, settings.pdf_dir)
    return await _queued(session, paper, workspace_uuid)


logger = logging.getLogger(__name__)


async def _add(session, providers, kind, value, workspace_uuid) -> AddPaperResult:
    if kind == "link":
        return await _add_link(session, providers, value, workspace_uuid)
    return await _add_found(session, providers, kind, value, workspace_uuid)


async def add_paper(session: AsyncSession, identifier: str, workspace: str | None = None) -> AddPaperResult:
    """Adds a paper by DOI, arXiv ID or direct PDF link, free copies only (D68, D217), and queues its ingest."""
    kind, value = _identifier(identifier)
    workspace_uuid = await workspace_id(session, workspace) if workspace is not None else None  # before any download
    async with discovery.open_providers(session) as providers:
        return await _add(session, providers, kind, value, workspace_uuid)


MAX_ADD_PAPERS = 50


@dataclass(frozen=True)
class AddPapersItem:
    identifier: str
    status: str | None = None
    paper_id: uuid.UUID | None = None
    error: str | None = None


def _distinct(identifiers: list[str]) -> list[tuple[str, tuple[str, str] | None, str | None]]:
    """(identifier, parsed, error) once per distinct paper, in order. An unparseable one is its own entry."""
    seen, out = set(), []
    for raw in identifiers:
        text = raw.strip()
        try:
            parsed, error = _identifier(text), None
        except InvalidInput as exc:
            parsed, error = None, error_code(exc)
        if (parsed or text) not in seen:
            seen.add(parsed or text)
            out.append((text, parsed, error))
    return out


async def add_papers(
    session: AsyncSession, identifiers: list[str], workspace: str | None = None
) -> list[AddPapersItem]:
    """add_paper for up to MAX_ADD_PAPERS identifiers with one set of providers; each item keeps its own error."""
    if len(identifiers) > MAX_ADD_PAPERS:
        raise InvalidInput("too_many_identifiers", max=MAX_ADD_PAPERS)
    workspace_uuid = await workspace_id(session, workspace) if workspace is not None else None  # before any download
    items = []
    async with discovery.open_providers(session) as providers:
        for text, parsed, error in _distinct(identifiers):
            if parsed is not None:
                try:
                    added = await _add(session, providers, *parsed, workspace_uuid)
                    items.append(AddPapersItem(text, added.status, added.paper_id))
                    continue
                except DomainError as exc:
                    error = error_code(exc)
                except Exception:  # one provider's surprise must not lose the items already added
                    logger.exception("add_papers failed on %s", text)
                    await session.rollback()  # a failed flush leaves the session unusable until then
                    error = "failed"
            items.append(AddPapersItem(text, error=error))
    return items


async def _sources(session: AsyncSession, sources: list[str] | None) -> list[str]:
    if sources:
        unknown = [s for s in sources if s not in DISCOVERY_SOURCE_IDS]
        if unknown:
            raise InvalidInput("unknown_source", unknown=unknown, valid=list(DISCOVERY_SOURCE_IDS))
        return sources
    on = (await paper_sources.get(session)).enabled
    chosen = [s for s in DISCOVERY_SOURCE_IDS if on.get(s)]
    if not chosen:
        raise InvalidInput("No paper source is on. Turn one on in Settings → Paper sources, or name `sources`.")
    return chosen


async def start_search_run(
    session: AsyncSession, workspace: str, query: str, sources: list[str] | None = None,
    year_from: int | None = None, year_to: int | None = None,
) -> SearchRunOut:
    ws = await workspace_id(session, workspace)
    if not query.strip():
        raise InvalidInput("Give the search a query.")
    filters = {"year_from": year_from, "year_to": year_to}
    run = await workspace_search.start_run(session, ws, query.strip(), filters, await _sources(session, sources))
    await queue.enqueue("run_workspace_search", str(run.id))
    return await workspace_search.run_out(session, run)


async def get_search_run(session: AsyncSession, run_id: uuid.UUID) -> SearchRunOut:
    return await workspace_search.run_out(session, await workspace_search.get_run(session, run_id))


async def list_search_runs(session: AsyncSession, workspace: str, limit: int = 20) -> list[SearchRunSummary]:
    runs = await workspace_search.list_runs(
        session, await workspace_id(session, workspace), max(1, min(limit, MAX_HITS))
    )
    return await workspace_search.run_summaries(session, runs)


async def stop_search_run(session: AsyncSession, run_id: uuid.UUID) -> SearchRunOut:
    return await workspace_search.run_out(session, await workspace_search.stop_run(session, run_id))


DECISIONS = ("relevant", "not_relevant", "maybe")
REASONS = ("wrong_topic", "wrong_study_type", "duplicate", "language", "inaccessible", "other")
MAX_HITS = 100


@dataclass(frozen=True)
class HitPage:
    hits: list[HitOut]
    next_cursor: str | None


@dataclass(frozen=True)
class ReviewResult:
    updated: int
    skipped: list[uuid.UUID]


async def list_hits(
    session: AsyncSession, workspace: str, run_id: uuid.UUID | None = None, status: str | None = None,
    cursor: str | None = None, limit: int = 50,
) -> HitPage:
    if status is not None and status not in (*DECISIONS, "undecided"):
        raise InvalidInput("invalid_decision", valid=[*DECISIONS, "undecided"])
    ws = await workspace_id(session, workspace)
    items, next_cursor = await workspace_search.list_hits(
        session, ws, limit=max(1, min(limit, MAX_HITS)), after=cursor, stage1_status=status, run_id=run_id
    )
    return HitPage(hits=[HitOut.model_validate(item) for item in items], next_cursor=next_cursor)


async def get_screening_suggestions(
    session: AsyncSession, workspace: str, run_id: uuid.UUID | None = None, min_relevance: int | None = None,
    cursor: str | None = None, limit: int = 50,
) -> HitPage:
    """Undecided hits the model has a suggestion for, best score first."""
    ws = await workspace_id(session, workspace)
    items, next_cursor = await workspace_search.list_hits(
        session, ws, limit=max(1, min(limit, MAX_HITS)), after=cursor, stage1_status="undecided", run_id=run_id,
        sort="ai", min_relevance=min_relevance, suggested_only=True,
    )
    return HitPage(hits=[HitOut.model_validate(item) for item in items], next_cursor=next_cursor)


async def _refuse_remote(session: AsyncSession, allow_remote: bool) -> None:
    """There is no confirm dialog over MCP: a remote default model runs only when the caller says the owner agreed."""
    model = await screening._default_model(session)
    if model is not None and not model[1] and not allow_remote:
        raise Conflict("remote_model_needs_confirm", model=model[0], host=model[2])


async def get_screening_criteria(session: AsyncSession, workspace: str) -> ScreeningStateOut:
    """The workspace's research question, inclusion and exclusion criteria (and the pre-screening state)."""
    return ScreeningStateOut.model_validate(await screening.get_state(session, await workspace_id(session, workspace)))


async def set_screening_criteria(
    session: AsyncSession, workspace: str, research_question: str | None = None,
    inclusion_criteria: str | None = None, exclusion_criteria: str | None = None,
) -> ScreeningStateOut:
    """Saves only the fields given (an empty string clears one), as the screening page does. A real change clears
    every stored AI suggestion. Refused while suggestions are being made: stop them first."""
    ws = await workspace_id(session, workspace)
    if (await screening.get_state(session, ws))["suggest_status"] != "idle":
        raise Conflict("suggestions_running", detail="Stop the AI suggestions before changing the criteria.")
    given = {key: value for key, value in (("research_question", research_question), ("inclusion", inclusion_criteria),
                                           ("exclusion", exclusion_criteria)) if value is not None}
    state = await screening.set_criteria(session, ws, **given) if given else await screening.get_state(session, ws)
    return ScreeningStateOut.model_validate(state)


PRESCREEN_CONFIRM_ABOVE = 500  # a workspace-wide run once queued 13,720 hits and ran for 4 hours


async def run_prescreen(
    session: AsyncSession, workspace: str, run_id: uuid.UUID | None = None, confirm: bool = False,
    allow_remote: bool = False, run_ids: list[uuid.UUID] | None = None,
) -> ScreeningStateOut:
    """Starts AI pre-screening. There is no confirm dialog over MCP, so a remote default model is refused unless
    allow_remote is set (the owner agreed), and so is a workspace-wide queue over PRESCREEN_CONFIRM_ABOVE hits
    unless confirm is set. The two are independent. run_ids queues several runs as one job."""
    if run_id is not None and run_ids is not None:
        raise InvalidInput("run_id_or_run_ids")
    if run_ids is not None and not run_ids:
        raise InvalidInput("run_ids_empty")
    ws = await workspace_id(session, workspace)
    run_ids = None if run_ids is None else list(dict.fromkeys(run_ids))
    for each in [run_id] if run_id is not None else run_ids or []:
        await workspace_search.get_run(session, each, workspace_id=ws)  # NotFound for a foreign run
    await _refuse_remote(session, allow_remote)
    if run_id is None and not confirm:
        # ponytail: loads the pool twice on a workspace-wide call (here and in start_suggestions); pass the count
        # through if it shows up in timings.
        queued = len(screening.suggestion_queue(await screening.load_pool(session, ws), run_ids=run_ids))
        if queued > PRESCREEN_CONFIRM_ABOVE:
            raise Conflict("prescreen_needs_confirm", queued=queued, limit=PRESCREEN_CONFIRM_ABOVE)
    state = await screening.start_suggestions(session, ws, allow_remote, run_id, run_ids=run_ids)
    if run_ids is None:
        await queue.enqueue("suggest_screening", str(ws))
    else:
        await queue.enqueue("suggest_screening", str(ws), [str(r) for r in run_ids])
    return ScreeningStateOut.model_validate(state)


@dataclass(frozen=True)
class EnrichQueued:
    queued: int
    already_running: bool = False
    suggestions_cleared: int = 0
    s2_off: bool = False  # Semantic Scholar is off in Settings: nothing was queued


async def enrich_hits(session: AsyncSession, workspace: str, run_id: uuid.UUID | None = None) -> EnrichQueued:
    """Queues the Semantic Scholar backfill for hits not asked about yet. `queued` is how many; calling again shows
    what a rate-limited run left, and `already_running` means a job for it is still going, so nothing new started.
    A hit Semantic Scholar couldn't fill is asked once only; arXiv gaps are asked
    again. With Semantic Scholar turned off in Settings nothing is queued (`s2_off`). Hits whose 'no abstract'
    suggestion is now out of date are cleared (`suggestions_cleared`), so run_prescreen picks them up again."""
    ws = await workspace_id(session, workspace)
    if run_id is not None:
        await workspace_search.get_run(session, run_id, workspace_id=ws)  # NotFound for a foreign run
    cleared = await hit_enrichment.clear_stale_no_abstract(session, ws)  # needs no Semantic Scholar
    if not (await paper_sources.get(session)).enabled["semantic_scholar"]:
        return EnrichQueued(0, suggestions_cleared=cleared, s2_off=True)
    s2_refs = await hit_enrichment.refs_to_enrich(session, ws, run_id)
    count = len({r.id for r in [*s2_refs, *await hit_enrichment.refs_for_arxiv(session, ws, run_id)]})
    if not count:
        return EnrichQueued(0, suggestions_cleared=cleared)
    # One job per workspace and run at a time: arq refuses a duplicate id while one is queued or running.
    started = await queue.enqueue(
        "enrich_hits", str(ws), str(run_id) if run_id else None, job_id=f"enrich_hits:{ws}:{run_id or 'all'}"
    )
    return EnrichQueued(count, already_running=not started, suggestions_cleared=cleared)


@dataclass(frozen=True)
class EnrichPapersQueued:
    queued: int
    already_running: bool = False
    s2_off: bool = False  # Semantic Scholar is off in Settings: nothing was queued


async def enrich_papers(session: AsyncSession, workspace: str) -> EnrichPapersQueued:
    """Queues the Semantic Scholar lookup of the workspace's papers' published versions. `queued` is how many papers
    have not been asked about yet; calling again shows what a rate-limited run left, and `already_running` means a
    job for it is still going, so nothing new started. With Semantic Scholar off in Settings nothing is queued."""
    ws = await workspace_id(session, workspace)
    if not (await paper_sources.get(session)).enabled["semantic_scholar"]:
        return EnrichPapersQueued(0, s2_off=True)
    count = len(await paper_enrichment.papers_to_enrich(session, ws))
    if not count:
        return EnrichPapersQueued(0)
    started = await queue.enqueue("enrich_papers", str(ws), job_id=f"enrich_papers:{ws}")
    return EnrichPapersQueued(count, already_running=not started)


async def stop_prescreen(session: AsyncSession, workspace: str) -> ScreeningStateOut:
    """Stops pre-screening; idle when nothing runs, and a second call on a stuck 'stopping' resets it."""
    state = await screening.stop_suggestions(session, await workspace_id(session, workspace))
    return ScreeningStateOut.model_validate(state)


MAX_ACCEPT_HITS = 5000  # the API's cap


async def accept_suggestions(
    session: AsyncSession, workspace: str, hit_ids: list[uuid.UUID], tags_: list[str] | None = None
) -> screening.AcceptResult:
    """Records the owner's decision from each suggestion: include becomes relevant, exclude not_relevant. `tags_`
    go on the accepted hits only."""
    if len(hit_ids) > MAX_ACCEPT_HITS:
        raise InvalidInput("too_many_hits", max=MAX_ACCEPT_HITS)
    for name in tags_ or ():
        tags.normalize(name)  # refuse a bad name before any decision is written
    ws = await workspace_id(session, workspace)
    accepted = await screening.accept_suggestions(session, ws, hit_ids)
    if tags_:
        skipped = set(accepted.skipped)
        await tags.tag_hits(session, ws, tags_, [h for h in hit_ids if h not in skipped])
    return accepted


async def review_hits(
    session: AsyncSession, workspace: str, hit_ids: list[uuid.UUID], decision: str, reason: str | None = None,
    tags_: list[str] | None = None,
) -> ReviewResult:
    """Records the owner's stage-1 decision (Claude Code asks before each call). Hits outside the workspace are
    skipped, never written (D218)."""
    if decision not in DECISIONS or (reason is not None and reason not in REASONS):
        raise InvalidInput("invalid_decision", decisions=list(DECISIONS), reasons=list(REASONS))
    if decision == "not_relevant" and reason is None:
        raise InvalidInput("reason_required", reasons=list(REASONS))
    for name in tags_ or ():
        tags.normalize(name)  # refuse a bad name before any decision is written
    ws = await workspace_id(session, workspace)
    own = await workspace_search.owned_hit_ids(session, ws, hit_ids)
    kept_reason = reason if decision == "not_relevant" else None
    updated = await workspace_search.bulk_review_hits(session, ws, list(own), decision, kept_reason, None)
    if tags_:
        await tags.tag_hits(session, ws, tags_, list(own))
    return ReviewResult(updated=updated, skipped=[h for h in hit_ids if h not in own])


async def import_hit(
    session: AsyncSession, workspace: str, hit_id: uuid.UUID, tags_: list[str] | None = None
) -> workspace_search.ImportResult:
    ws = await workspace_id(session, workspace)
    if hit_id not in await workspace_search.owned_hit_ids(session, ws, [hit_id]):
        raise NotFound("hit_not_found", hit_id=str(hit_id))
    if tags_:
        await tags.tag_hits(session, ws, tags_, [hit_id])
    async with discovery.open_providers(session) as providers:
        imported = await workspace_search.import_hits(session, providers, ws, [hit_id])
    for paper_id in imported.paper_ids:
        await queue.enqueue("ingest_paper", str(paper_id))
    return imported


DIRECTIONS = {"backward": (True, False), "forward": (False, True), "both": (True, True)}
VERDICTS = ("include", "exclude")


@dataclass(frozen=True)
class EligibilityView:
    paper_id: uuid.UUID
    run_id: uuid.UUID
    verdict: str | None
    reason: str | None
    assessed_at: datetime | None


async def snowball(
    session: AsyncSession, workspace: str, seed_paper_ids: list[uuid.UUID], direction: str
) -> workspace_search.SnowballResult:
    if direction not in DIRECTIONS:
        raise InvalidInput(f"direction is one of {', '.join(DIRECTIONS)}")
    if not seed_paper_ids:
        raise InvalidInput("Give at least one seed paper.")
    backward, forward = DIRECTIONS[direction]
    ws = await workspace_id(session, workspace)
    async with discovery.open_providers(session) as providers:
        return await workspace_search.snowball(session, providers, ws, seed_paper_ids, [], backward, forward)


async def set_eligibility(
    session: AsyncSession, workspace: str, paper_id: uuid.UUID, run_id: uuid.UUID, verdict: str,
    reason: str | None = None,
) -> EligibilityView:
    if verdict not in VERDICTS:
        raise InvalidInput("invalid_decision", verdicts=list(VERDICTS))
    ws = await workspace_id(session, workspace)
    row = await workspace_search.set_eligibility(session, ws, paper_id, run_id, verdict, reason)
    return EligibilityView(
        row.paper_id, row.search_run_id, row.stage2_status, row.stage2_exclude_reason, row.assessed_at
    )


async def get_prisma(
    session: AsyncSession, workspace: str, run_id: uuid.UUID | None = None
) -> prisma_export.PrismaExport:
    return await prisma_export.prisma_export(session, await workspace_id(session, workspace), run_id)


AUTHORS = {"you": {"human"}, "ai": {"llm", "llm_edited"}, "all": {"human", "llm", "llm_edited"}}
LABELS = {"human": "You", "llm": "AI", "llm_edited": "AI · edited"}


@dataclass(frozen=True)
class LabelledNote(notes.NoteView):
    author: str = ""


async def list_notes(
    session: AsyncSession, workspace: str | None = None, paper_id: uuid.UUID | None = None, author: str = "all",
    tags_: list[str] | None = None, match: Literal["any", "all"] = "any",
) -> list[LabelledNote]:
    if (workspace is None) == (paper_id is None):
        raise InvalidInput("notes_scope", hint="Give either workspace or paper_id.")
    if tags_ and workspace is None:
        raise InvalidInput("tags_need_a_workspace")
    if author not in AUTHORS:
        raise InvalidInput(f"author is one of {', '.join(AUTHORS)}")
    wanted = None
    if workspace is not None:
        ws = await workspace_id(session, workspace)
        wanted = await tags.paper_ids(session, ws, tags_, match)
        views = await workspaces.notes(session, ws)
    else:
        views = await notes.list_notes_for_paper(session, paper_id)
    return [
        LabelledNote(**{f.name: getattr(v, f.name) for f in fields(v)}, author=LABELS[v.provenance])
        for v in views
        if v.provenance in AUTHORS[author] and (wanted is None or wanted.intersection(v.paper_ids))
    ]


async def create_saved_search(
    session: AsyncSession, workspace: str, query: str, schedule: str = "daily", time: str | None = "02:00",
    weekday: int | None = None, cron: str | None = None, sources: list[str] | None = None,
    year_from: int | None = None, year_to: int | None = None, timezone: str = "UTC",
) -> saved_searches.SavedSearchView:
    ws = await workspace_id(session, workspace)
    if (cron is not None and schedule != "cron") or (weekday is not None and schedule != "weekly"):
        raise InvalidInput("invalid_schedule", hint="cron needs schedule='cron'; weekday needs schedule='weekly'.")
    expression = schedules.to_cron(schedule, time=time, weekday=weekday, cron=cron)
    return await saved_searches.create(
        session, ws, query=query, schedule=expression, timezone=timezone, sources=sources or [],
        year_from=year_from, year_to=year_to,
    )


async def list_saved_searches(session: AsyncSession, workspace: str) -> list[saved_searches.SavedSearchView]:
    return await saved_searches.list_for_workspace(session, await workspace_id(session, workspace))


async def set_saved_search_enabled(
    session: AsyncSession, saved_search_id: uuid.UUID, enabled: bool
) -> saved_searches.SavedSearchView:
    view = await saved_searches.get(session, saved_search_id)
    return await saved_searches.update(session, view.workspace_id, saved_search_id, {"enabled": enabled})


async def verify_claim(
    session: AsyncSession, claim: str, paper_id: uuid.UUID | None = None, doi: str | None = None,
    quoted_text: str | None = None, save: bool = False, allow_remote: bool = False,
) -> claims.ClaimCheck:
    await _refuse_remote(session, allow_remote)
    try:
        return await claims.verify_claim(
            session, claim, paper_id=paper_id, doi=doi, quoted_text=quoted_text, save=save
        )
    except (LLMError, LLMUnavailable) as exc:
        raise Conflict("model_unavailable", detail=str(exc)) from exc


async def verify_claims(
    session: AsyncSession, text: str, workspace: str | None = None, save: bool = False, allow_remote: bool = False
) -> list[claims.ClaimItem]:
    ws = await workspace_id(session, workspace) if workspace is not None else None
    await _refuse_remote(session, allow_remote)
    return await claims.verify_claims(session, text, workspace_id=ws, save=save)


@dataclass(frozen=True)
class EvidenceRow:
    paper_id: uuid.UUID
    title: str
    year: int | None
    first_author: str | None
    card: evidence_cards.CardSummary | None


async def generate_evidence_card(
    session: AsyncSession, paper_id: uuid.UUID, regenerate: bool = False, confirm: bool = False,
    allow_remote: bool = False,
) -> evidence_cards.CardSummary:
    """Starts the background job and returns the card at once (`generating`); the caller polls get_evidence_card."""
    await _refuse_remote(session, allow_remote)
    [view] = await evidence_cards.request_cards(session, paper_ids=[paper_id], regenerate=regenerate, confirm=confirm)
    return evidence_cards.summarize(view)


async def get_evidence_card(session: AsyncSession, paper_id: uuid.UUID) -> evidence_cards.CardSummary:
    return evidence_cards.summarize(await evidence_cards.get_card(session, paper_id))


async def list_evidence_cards(
    session: AsyncSession, workspace: str, tags_: list[str] | None = None, match: Literal["any", "all"] = "any"
) -> list[EvidenceRow]:
    ws = await workspace_id(session, workspace)
    wanted = await tags.paper_ids(session, ws, tags_, match)
    rows = await evidence_cards.workspace_cards(session, ws)
    return [
        EvidenceRow(r.paper_id, r.title, r.year, r.first_author, r.card and evidence_cards.summarize(r.card))
        for r in rows
        if wanted is None or r.paper_id in wanted
    ]


async def export_citations(
    session: AsyncSession, *, workspace_id: uuid.UUID | None = None, paper_ids: list[uuid.UUID] | None = None, fmt: str
) -> tuple[str, int]:
    """(text, paper count) for a workspace or a selection of papers; shared by the API and the MCP tool."""
    return await citations_export.export_papers(session, workspace_id=workspace_id, paper_ids=paper_ids, fmt=fmt)


@dataclass(frozen=True)
class CitationExport:
    format: str
    count: int
    text: str


async def export_citations_as(
    session: AsyncSession, workspace: str, fmt: str, tags_: list[str] | None = None,
    match: Literal["any", "all"] = "any",
) -> CitationExport:
    ws = await workspace_id(session, workspace)
    wanted = await tags.paper_ids(session, ws, tags_, match)
    # export_papers takes a workspace or paper ids, not both
    scope = {"workspace_id": ws}
    if wanted is not None:
        scope = {"paper_ids": [p.id for p in await workspaces.papers(session, ws) if p.id in wanted]}
    text, count = await export_citations(session, fmt=fmt, **scope)
    return CitationExport(fmt, count, text)
