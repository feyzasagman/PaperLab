"""What Claude does over MCP (M39 F1): each function backs one tool, composing existing core services.

Workspaces come by name (D93). Jobs go through `queue`, paper sources through `discovery.open_providers`. No SQL here:
every read and write is an existing service's. Results are the API's own schemas, so Claude and the app see the same
fields.
"""

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
    notes,
    paper_sources,
    papers,
    pdf_links,
    prisma_export,
    queue,
    saved_searches,
    schedules,
    screening,
    workspace_search,
    workspaces,
)
from app.core.errors import Conflict, InvalidInput, NotFound
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


async def list_papers(session: AsyncSession, workspace: str) -> list[PaperOut]:
    rows = await workspaces.papers(session, await workspace_id(session, workspace))
    return [PaperOut.model_validate(row) for row in rows]


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
    providers.pdf.event_hooks["request"].append(pdf_links.guard)  # this call's own client, closed after it
    data = await discovery.download_pdf(providers.pdf, [link])
    if data is None:
        return AddPaperResult(status="no_free_pdf", landing_url=link)
    # Like an upload: extraction and enrichment find the metadata. A paper already in the library becomes a second
    # copy, exactly as uploading the same PDF twice does (enrichment drops a DOI another paper holds).
    name = PurePosixPath(urlsplit(link).path).name or "paper.pdf"
    paper = await papers.create_paper(session, name, data, settings.pdf_dir)
    return await _queued(session, paper, workspace_uuid)


async def add_paper(session: AsyncSession, identifier: str, workspace: str | None = None) -> AddPaperResult:
    """Adds a paper by DOI, arXiv ID or direct PDF link, free copies only (D68, D217), and queues its ingest."""
    kind, value = _identifier(identifier)
    workspace_uuid = await workspace_id(session, workspace) if workspace is not None else None  # before any download
    async with discovery.open_providers(session) as providers:
        if kind == "link":
            return await _add_link(session, providers, value, workspace_uuid)
        return await _add_found(session, providers, kind, value, workspace_uuid)


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
    return SearchRunOut.model_validate(run)


async def get_search_run(session: AsyncSession, run_id: uuid.UUID) -> SearchRunOut:
    return SearchRunOut.model_validate(await workspace_search.get_run(session, run_id))


async def list_search_runs(session: AsyncSession, workspace: str, limit: int = 20) -> list[SearchRunSummary]:
    runs = await workspace_search.list_runs(session, await workspace_id(session, workspace), limit)
    return [SearchRunSummary.from_run(run, count) for run, count in runs]


async def stop_search_run(session: AsyncSession, run_id: uuid.UUID) -> SearchRunOut:
    return SearchRunOut.model_validate(await workspace_search.stop_run(session, run_id))


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
        raise Conflict("remote_model_needs_confirm", model=model[0])


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


async def run_prescreen(session: AsyncSession, workspace: str, run_id: uuid.UUID | None = None) -> ScreeningStateOut:
    """Starts AI pre-screening. There is no confirm dialog over MCP, so a remote default model is refused."""
    ws = await workspace_id(session, workspace)
    if run_id is not None:
        await workspace_search.get_run(session, run_id, workspace_id=ws)  # NotFound for a foreign run
    await _refuse_remote(session, False)
    state = await screening.start_suggestions(session, ws, False, run_id)
    await queue.enqueue("suggest_screening", str(ws))
    return ScreeningStateOut.model_validate(state)


MAX_ACCEPT_HITS = 5000  # the API's cap


async def accept_suggestions(
    session: AsyncSession, workspace: str, hit_ids: list[uuid.UUID]
) -> screening.AcceptResult:
    """Records the owner's decision from each suggestion: include becomes relevant, exclude not_relevant."""
    if len(hit_ids) > MAX_ACCEPT_HITS:
        raise InvalidInput("too_many_hits", max=MAX_ACCEPT_HITS)
    return await screening.accept_suggestions(session, await workspace_id(session, workspace), hit_ids)


async def review_hits(
    session: AsyncSession, workspace: str, hit_ids: list[uuid.UUID], decision: str, reason: str | None = None
) -> ReviewResult:
    """Records the owner's stage-1 decision (Claude Code asks before each call). Hits outside the workspace are
    skipped, never written (D218)."""
    if decision not in DECISIONS or (reason is not None and reason not in REASONS):
        raise InvalidInput("invalid_decision", decisions=list(DECISIONS), reasons=list(REASONS))
    if decision == "not_relevant" and reason is None:
        raise InvalidInput("reason_required", reasons=list(REASONS))
    ws = await workspace_id(session, workspace)
    own = await workspace_search.owned_hit_ids(session, ws, hit_ids)
    kept_reason = reason if decision == "not_relevant" else None
    updated = await workspace_search.bulk_review_hits(session, ws, list(own), decision, kept_reason, None)
    return ReviewResult(updated=updated, skipped=[h for h in hit_ids if h not in own])


async def import_hit(session: AsyncSession, workspace: str, hit_id: uuid.UUID) -> workspace_search.ImportResult:
    ws = await workspace_id(session, workspace)
    if hit_id not in await workspace_search.owned_hit_ids(session, ws, [hit_id]):
        raise NotFound("hit_not_found", hit_id=str(hit_id))
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
    session: AsyncSession, workspace: str | None = None, paper_id: uuid.UUID | None = None, author: str = "all"
) -> list[LabelledNote]:
    if (workspace is None) == (paper_id is None):
        raise InvalidInput("notes_scope", hint="Give either workspace or paper_id.")
    if author not in AUTHORS:
        raise InvalidInput(f"author is one of {', '.join(AUTHORS)}")
    views = (
        await workspaces.notes(session, await workspace_id(session, workspace))
        if workspace is not None
        else await notes.list_notes_for_paper(session, paper_id)
    )
    return [
        LabelledNote(**{f.name: getattr(v, f.name) for f in fields(v)}, author=LABELS[v.provenance])
        for v in views
        if v.provenance in AUTHORS[author]
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


async def list_evidence_cards(session: AsyncSession, workspace: str) -> list[EvidenceRow]:
    rows = await evidence_cards.workspace_cards(session, await workspace_id(session, workspace))
    return [
        EvidenceRow(r.paper_id, r.title, r.year, r.first_author, r.card and evidence_cards.summarize(r.card))
        for r in rows
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


async def export_citations_as(session: AsyncSession, workspace: str, fmt: str) -> CitationExport:
    text, count = await export_citations(session, workspace_id=await workspace_id(session, workspace), fmt=fmt)
    return CitationExport(fmt, count, text)
