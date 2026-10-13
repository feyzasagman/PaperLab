"""A library paper's references and citing works: fetched once, stored, ranked for this library (M7.5).

- Semantic Scholar answers both directions; OpenAlex is merged in when it's ticked (D77). Stored in `external_refs`
  and `paper_references`, so ranking can count how many library papers share a reference (D78).
- Ranking is tiers (D80): co-citation inside the library, closeness to the reader's notes, a free PDF, citation count.
  A reference is in the library by `IN_LIBRARY`, the one match the tab, the References page and the graph share.
- Importing downloads a free PDF exactly as Find papers' Add does (D84); nothing is fetched for the new paper (P5).
"""

import dataclasses
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import discovery, papers, workspaces
from app.core.candidates import Candidate, from_s2, from_work, merge, ordered_pdf_urls
from app.core.errors import Conflict, NotFound
from app.models import ExternalRef, Note, NoteEmbedding, Paper, paper_references
from app.providers import embedding, openalex, semantic_scholar

logger = logging.getLogger(__name__)

REFS_CAP = 500
CITING_CAP = 200
CO_CITATION_SUMMARY = 3
COCITED_MIN = 2  # D166, D177: the References page's threshold for both directions
# A fetch the worker never finished (a restart mid-job) can be asked for again after this long.
FETCH_STALE = timedelta(minutes=10)

FETCH_FAILED = "Fetching references failed. Try again."
REFERENCES_OFF = "Semantic Scholar is off. Turn it on in Settings → Paper sources to see references."
REFERENCES_UNKNOWN = "Semantic Scholar doesn't know this paper, so it can't list its references."
# Two papers' fetches can store the same new reference, or embed the same notes, at once: a unique violation or a
# deadlock fails one of them. Storing and embedding take this transaction-scoped advisory lock first.
# ponytail: one references write at a time across the library; per-reference locks if that ever matters.
REFERENCES_LOCK = 7_500_001
_TAKE_LOCK = text("SELECT pg_advisory_xact_lock(:key)")


@dataclass(frozen=True)
class ReferenceRow:
    id: uuid.UUID
    title: str
    authors: list[str]
    year: int | None
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    openalex_id: str | None
    s2_id: str | None
    cited_by_count: int | None
    has_pdf: bool
    cocitation: int  # library papers citing it (or, for citing works, library papers it cites)
    paper_id: uuid.UUID | None  # set when the library holds it
    position: int
    queued_at: datetime | None  # when the reader marked it To read (D164)


@dataclass(frozen=True)
class Summary:
    cited_by_3plus: int
    with_pdf: int


@dataclass(frozen=True)
class Listing:
    state: str
    error: str | None
    direction: str
    summary: Summary
    rows: list[ReferenceRow]


# --- fetch -------------------------------------------------------------------------------------------------------


async def _from_semantic_scholar(providers: discovery.Providers, paper: Paper) -> dict[str, list[Candidate]] | None:
    """None when Semantic Scholar doesn't know the paper."""
    key = await discovery.s2_key(providers.client("semantic_scholar"), paper.doi, paper.title)
    if key is None:
        return None
    cites = await semantic_scholar.references(providers.client("semantic_scholar"), key, REFS_CAP)
    if cites is None:
        return None
    cited_by = await semantic_scholar.citations(providers.client("semantic_scholar"), key, CITING_CAP) or []
    return {"cites": [from_s2(p) for p in cites], "cited_by": [from_s2(p) for p in cited_by]}


async def _from_openalex(providers: discovery.Providers, paper: Paper) -> dict[str, list[Candidate]]:
    ids = await openalex.referenced_works(providers.client("openalex"), paper.openalex_id)
    cites = await openalex.works_by_ids(providers.client("openalex"), ids[:REFS_CAP])
    cited_by = await openalex.citing_works(providers.client("openalex"), paper.openalex_id, CITING_CAP)
    return {"cites": [from_work(w) for w in cites], "cited_by": [from_work(w) for w in cited_by]}


async def fetch(session: AsyncSession, providers: discovery.Providers, paper: Paper) -> list[str]:
    """Stores both directions for `paper` and returns a notice per source that failed. Raises Conflict when no
    source could answer: none is on, the sources don't know the paper, or every one failed."""
    found: dict[str, dict[str, list[Candidate]]] = {}  # source -> direction -> candidates, most trusted first
    notices: list[str] = []
    unknown = False
    asks = []
    if providers.client("openalex") is not None and paper.openalex_id:
        asks.append(("openalex", _from_openalex))
    if providers.client("semantic_scholar") is not None:
        asks.append(("semantic_scholar", _from_semantic_scholar))
    if not asks:
        raise Conflict(REFERENCES_OFF)
    for source, ask in asks:
        try:
            answer = await ask(providers, paper)
        except httpx.HTTPError as exc:
            logger.info("%s failed for the references of %s: %s", source, paper.id, exc)
            notices.append(discovery.notice(source, exc))
            continue
        if answer is None:
            unknown = True
        else:
            found[source] = answer
    if not found:
        raise Conflict(" ".join(notices) if notices else REFERENCES_UNKNOWN if unknown else FETCH_FAILED)
    await session.execute(_TAKE_LOCK, {"key": REFERENCES_LOCK})  # after the network calls: held until the commit
    for direction, cap in (("cites", REFS_CAP), ("cited_by", CITING_CAP)):
        # A record with no id at all (Semantic Scholar lists some) can't be matched, imported or co-cited: not stored.
        identified = {
            source: [c for c in lists[direction] if _same_reference(c.external_ids, c.doi)]
            for source, lists in found.items()
        }
        merged = merge(identified, cap)
        if direction == "cited_by":  # newest first, as the reverse direction is read (addendum §3d)
            merged = sorted(merged, key=lambda c: -(c.year or 0))
        await session.execute(
            delete(paper_references).where(
                paper_references.c.paper_id == paper.id, paper_references.c.direction == direction
            )
        )
        for position, candidate in enumerate(merged):
            # Linked right after its row is stored, so a later candidate that folds this row moves the link with it.
            ref_id = await _upsert(session, candidate)
            link = {"paper_id": paper.id, "ref_id": ref_id, "direction": direction, "position": position}
            await session.execute(pg_insert(paper_references).values(**link).on_conflict_do_nothing())
    await session.commit()
    return notices


async def _upsert(session: AsyncSession, candidate: Candidate) -> uuid.UUID:
    """The stored reference for `candidate`, created or refreshed. Rows that turn out to be the same paper (one known
    by its Semantic Scholar id, another by its DOI) fold into the oldest, and an imported row always wins."""
    matches = _same_reference(candidate.external_ids, candidate.doi)
    existing = []
    if matches:  # or_() of nothing compiles to no WHERE, which would fold every stored reference into this one
        existing = list(
            await session.scalars(
                select(ExternalRef)
                .where(or_(*matches))
                .order_by(ExternalRef.imported_as.is_(None), ExternalRef.fetched_at, ExternalRef.id)
            )
        )
    fields = {
        "title": candidate.title,
        "authors": candidate.authors,
        "year": candidate.year,
        "venue": candidate.venue,
        "cited_by_count": candidate.cited_by_count,
    }
    if not existing:
        return await session.scalar(
            insert(ExternalRef)
            .values(**fields, pdf_urls=candidate.pdf_urls, doi=candidate.doi, external_ids=dict(candidate.external_ids))
            .returning(ExternalRef.id)
        )
    keeper, *duplicates = existing
    # Candidate's own value wins; a duplicate fills what's still missing; the keeper's pre-existing value fills
    # what's still missing after that — the same three-tier precedence the old per-field `ids[name] = ids[name]
    # or getattr(duplicate, name)` loop gave, now as dict merges since "key absent" replaces "value is None".
    external_ids = dict(candidate.external_ids)
    doi = candidate.doi
    for duplicate in duplicates:
        external_ids = {**duplicate.external_ids, **external_ids}
        doi = doi or duplicate.doi
        await _repoint(session, duplicate.id, keeper.id)
        await session.delete(duplicate)
    await session.flush()
    external_ids = {**keeper.external_ids, **external_ids}
    doi = doi or keeper.doi
    if candidate.title != keeper.title:  # a new title needs a new vector
        fields |= {"title_embedding": None, "title_embed_model": None}
    # Preserve PDF URLs from candidate, duplicates, and keeper when folding
    fields["pdf_urls"] = ordered_pdf_urls(
        external_ids.get("arxiv"),
        *candidate.pdf_urls,
        *(url for row in [*duplicates, keeper] for url in row.pdf_urls),
    )
    earliest = min((row.queued_at for row in duplicates if row.queued_at is not None), default=None)
    if earliest is not None:  # D120: a fold keeps the earliest To read; the keeper's own value is read in SQL
        fields = {**fields, "queued_at": func.least(ExternalRef.queued_at, earliest)}
    await session.execute(
        update(ExternalRef).where(ExternalRef.id == keeper.id).values(**fields, doi=doi, external_ids=external_ids)
    )
    return keeper.id


def _same_reference(external_ids: dict[str, str], doi: str | None) -> list:
    """Conditions matching any stored reference that shares an id; [] when there is no id to match on."""
    conditions = [ExternalRef.external_ids[source].astext == value for source, value in external_ids.items()]
    if doi:
        conditions.append(func.lower(ExternalRef.doi) == doi.lower())
    return conditions


async def _repoint(session: AsyncSession, old: uuid.UUID, new: uuid.UUID) -> None:
    """Moves every paper's link from a duplicate reference to the row it folds into, keeping the first position."""
    await session.execute(
        text(
            "INSERT INTO paper_references (paper_id, ref_id, direction, position) "
            "SELECT paper_id, :new, direction, position FROM paper_references WHERE ref_id = :old "
            "ON CONFLICT DO NOTHING"
        ),
        {"old": old, "new": new},
    )
    await session.execute(delete(paper_references).where(paper_references.c.ref_id == old))


async def request_fetch(session: AsyncSession, paper_id: uuid.UUID) -> bool:
    """Marks the paper's references as fetching and returns whether a job should be queued: False while a recent
    fetch is still running, so a second click queues nothing."""
    await papers.get_paper(session, paper_id)
    stale = datetime.now(timezone.utc) - FETCH_STALE
    claimed = await session.scalar(
        update(Paper)
        .where(
            Paper.id == paper_id,
            or_(
                Paper.references_state != "fetching",
                Paper.references_requested_at.is_(None),
                Paper.references_requested_at < stale,
            ),
        )
        .values(references_state="fetching", references_error=None, references_requested_at=func.now())
        .returning(Paper.id)
    )
    await session.commit()
    return claimed is not None


async def set_state(session: AsyncSession, paper_id: uuid.UUID, state: str, error: str | None = None) -> None:
    await session.execute(
        update(Paper).where(Paper.id == paper_id).values(references_state=state, references_error=error)
    )
    await session.commit()


# --- To read (D164) ----------------------------------------------------------------------------------------------


async def queue(session: AsyncSession, ref_id: uuid.UUID) -> datetime:
    """Marks a reference To read. A second call keeps the first time. Nothing but this, a fold and an import writes
    queued_at, so a Refresh keeps it. Raises NotFound."""
    queued_at = await session.scalar(
        update(ExternalRef)
        .where(ExternalRef.id == ref_id)
        .values(queued_at=func.coalesce(ExternalRef.queued_at, func.now()))
        .returning(ExternalRef.queued_at)
    )
    if queued_at is None:
        raise NotFound(f"reference {ref_id} not found")
    await session.commit()
    return queued_at


async def unqueue(session: AsyncSession, ref_id: uuid.UUID) -> None:
    """Takes a reference off To read. Harmless when it isn't on it. Raises NotFound."""
    found = await session.scalar(
        update(ExternalRef).where(ExternalRef.id == ref_id).values(queued_at=None).returning(ExternalRef.id)
    )
    if found is None:
        raise NotFound(f"reference {ref_id} not found")
    await session.commit()


# --- embeddings --------------------------------------------------------------------------------------------------


EMBED_BATCH = 64  # titles embedded, and committed, per lock hold


async def embed_new(session: AsyncSession, embedder) -> None:
    """Vectors for reference titles and notes that have none, or were made by another source or before an edit, under
    the embedder's name. With no search model (embedder None) there are none to make: the listing ranks by its other
    signals (D136). Every source embeds them, cloud or not (P3 = A, D161): no source-specific branch."""
    if embedder is None:
        return
    model_name = embedder.name
    # Only references a listing can rank need a vector: cited by or citing a library paper, or on To read. Bulk
    # harvests store 100k+ others; embedding them under the lock stalled every fetch. Batches commit, so the lock
    # is held for one batch at a time.
    needed = or_(
        ExternalRef.queued_at.is_not(None),
        select(paper_references.c.ref_id).where(paper_references.c.ref_id == ExternalRef.id).exists(),
    )
    while True:
        await session.execute(_TAKE_LOCK, {"key": REFERENCES_LOCK})
        refs = (
            await session.execute(
                select(ExternalRef.id, ExternalRef.title)
                .where(or_(ExternalRef.title_embedding.is_(None), ExternalRef.title_embed_model != model_name), needed)
                .limit(EMBED_BATCH)
            )
        ).all()
        if not refs:
            break
        vectors = await embedding.embed_documents(embedder, [title for _, title in refs])
        for (ref_id, _), vector in zip(refs, vectors):
            await session.execute(
                update(ExternalRef)
                .where(ExternalRef.id == ref_id)
                .values(title_embedding=vector, title_embed_model=model_name)
            )
        await session.commit()
    await session.execute(_TAKE_LOCK, {"key": REFERENCES_LOCK})
    # ponytail: every fetch re-checks every note; with thousands of notes, embed on note save instead.
    notes = (
        await session.execute(
            select(Note.id, Note.body, Note.updated_at)
            .outerjoin(NoteEmbedding, NoteEmbedding.note_id == Note.id)
            .where(
                or_(
                    NoteEmbedding.note_id.is_(None),
                    NoteEmbedding.noted_at < Note.updated_at,
                    NoteEmbedding.embed_model != model_name,
                )
            )
        )
    ).all()
    if notes:
        vectors = await embedding.embed_documents(embedder, [body for _, body, _ in notes])
        for (note_id, _, updated_at), vector in zip(notes, vectors):
            await session.merge(
                NoteEmbedding(note_id=note_id, embedding=vector, embed_model=model_name, noted_at=updated_at)
            )
    await session.commit()


# --- listing -----------------------------------------------------------------------------------------------------

# The one in-library match (D121, D165): the tab, the References page and the graph all compose this. A reference is a
# library paper when it was imported as one, or a paper has its OpenAlex ID, its DOI in any case, or its arXiv DOI.
# openalex_id/arxiv_id come from external_ids (Phase 0b), not the legacy columns of the same name — those stay
# mapped on the model but are no longer written by anything, so a reference discovered after this migration would
# never match here if this still joined on them directly.
IN_LIBRARY = """in_library(ref_id, paper_id) AS (
  SELECT id, imported_as FROM external_refs WHERE imported_as IS NOT NULL
  UNION SELECT r.id, p.id FROM external_refs r JOIN papers p ON p.openalex_id = r.external_ids->>'openalex'
         WHERE r.imported_as IS NULL
  UNION SELECT r.id, p.id FROM external_refs r JOIN papers p ON lower(p.doi) = lower(r.doi)
         WHERE r.imported_as IS NULL
  UNION SELECT r.id, p.id FROM external_refs r
           JOIN papers p ON lower(p.doi) = '10.48550/arxiv.' || lower(r.external_ids->>'arxiv')
         WHERE r.imported_as IS NULL
)"""

# ponytail: no vector index; at library scale (hundreds of refs, tens of notes) a scan beats an HNSW build. Add one
# when a library passes ~50k references.
NOTE_SIMILARITY = """(SELECT max(1 - (r.title_embedding <=> n.embedding)) FROM note_embeddings n
             WHERE n.embed_model = r.title_embed_model)"""

# D80's tiers, written once. Bare output-column names, so Postgres reads them as the SELECT's own columns in both
# statements (pr.position in the tab, l.position on the page).
RANK = "cocitation DESC, note_similarity DESC NULLS LAST, has_pdf DESC, cited_by_count DESC NULLS LAST, position"

_LISTING = text(
    f"""
    WITH {IN_LIBRARY}
    SELECT r.id, r.title, r.authors, r.year, r.venue, r.doi,
           r.external_ids->>'arxiv' AS arxiv_id, r.external_ids->>'openalex' AS openalex_id,
           r.external_ids->>'semantic_scholar' AS s2_id, r.cited_by_count,
           jsonb_array_length(r.pdf_urls) > 0 AS has_pdf,
           (SELECT count(DISTINCT other.paper_id) FROM paper_references other
             WHERE other.ref_id = r.id AND other.direction = pr.direction) AS cocitation,
           {NOTE_SIMILARITY} AS note_similarity,
           (SELECT il.paper_id FROM in_library il WHERE il.ref_id = r.id ORDER BY il.paper_id LIMIT 1) AS paper_id,
           pr.position, r.queued_at
      FROM paper_references pr
      JOIN external_refs r ON r.id = pr.ref_id
     WHERE pr.paper_id = :paper_id AND pr.direction = :direction
     ORDER BY {RANK}
    """
)


def shown_state(state: str, error: str | None, requested_at: datetime | None) -> tuple[str, str | None]:
    """The 10-minute stale rule (a lost job, e.g. a worker restart mid-fetch): shown as failed, so Try again claims
    it as request_fetch does. Shared by the tab's listing and the References page's coverage."""
    if state == "fetching" and (requested_at is None or requested_at < datetime.now(timezone.utc) - FETCH_STALE):
        return "failed", FETCH_FAILED
    return state, error


async def listing(session: AsyncSession, paper_id: uuid.UUID, direction: str) -> Listing:
    paper = await papers.get_paper(session, paper_id)
    rows = [
        ReferenceRow(**{key: value for key, value in row._mapping.items() if key != "note_similarity"})
        for row in await session.execute(_LISTING, {"paper_id": paper_id, "direction": direction})
    ]
    state, error = shown_state(paper.references_state, paper.references_error, paper.references_requested_at)
    return Listing(
        state=state,
        error=error,
        direction=direction,
        summary=summarize(rows),
        rows=rows,
    )


def summarize(rows: Sequence[ReferenceRow]) -> Summary:
    return Summary(
        cited_by_3plus=sum(row.cocitation >= CO_CITATION_SUMMARY for row in rows),
        with_pdf=sum(row.has_pdf for row in rows),
    )


@dataclass(frozen=True)
class UnfetchedPaper:
    id: uuid.UUID
    title: str
    state: str  # none | fetching | failed (shown state)
    error: str | None


@dataclass(frozen=True)
class Coverage:
    fetched: int
    total: int
    unfetched: list[UnfetchedPaper]  # by title, then id


@dataclass(frozen=True)
class ReferencePage:
    coverage: Coverage
    to_read: list[ReferenceRow]  # newest queued_at first
    cited_by_several: list[ReferenceRow]  # D80 order
    citing_several: list[ReferenceRow]  # newest first (D177); cocitation = scope papers it cites


_PAGE = text(
    f"""
    WITH {IN_LIBRARY}, scope(paper_id) AS (
      SELECT p.id FROM papers p
       WHERE CAST(:workspace AS uuid) IS NULL
          OR EXISTS (SELECT 1 FROM workspace_papers wp WHERE wp.paper_id = p.id AND wp.workspace_id = :workspace)
    ), linked(ref_id, cocitation, citing, position) AS (
      SELECT pr.ref_id,
             count(DISTINCT pr.paper_id) FILTER (WHERE pr.direction = 'cites'),
             count(DISTINCT pr.paper_id) FILTER (WHERE pr.direction = 'cited_by'),
             min(pr.position)
        FROM paper_references pr JOIN scope s ON s.paper_id = pr.paper_id
       GROUP BY pr.ref_id
    )
    SELECT r.id, r.title, r.authors, r.year, r.venue, r.doi,
           r.external_ids->>'arxiv' AS arxiv_id, r.external_ids->>'openalex' AS openalex_id,
           r.external_ids->>'semantic_scholar' AS s2_id, r.cited_by_count,
           jsonb_array_length(r.pdf_urls) > 0 AS has_pdf, coalesce(l.cocitation, 0) AS cocitation,
           coalesce(l.citing, 0) AS citing, {NOTE_SIMILARITY} AS note_similarity,
           NULL::uuid AS paper_id, coalesce(l.position, 0) AS position, r.queued_at
      FROM external_refs r LEFT JOIN linked l ON l.ref_id = r.id
     WHERE NOT EXISTS (SELECT 1 FROM in_library il WHERE il.ref_id = r.id)
       AND (l.ref_id IS NOT NULL OR (r.queued_at IS NOT NULL AND CAST(:workspace AS uuid) IS NULL))
       AND (r.queued_at IS NOT NULL OR l.cocitation >= :cocited_min OR l.citing >= :cocited_min)
     ORDER BY {RANK}, r.id
    """
)


async def library_listing(session: AsyncSession, workspace_id: uuid.UUID | None = None) -> ReferencePage:
    """The References page (D121, D166): To read, works several scope papers cite, and recent works that cite
    several of them (D177), across the library or one workspace. Raises NotFound for an unknown workspace."""
    if workspace_id is not None:
        await workspaces.get(session, workspace_id)
    raw = list(await session.execute(_PAGE, {"workspace": workspace_id, "cocited_min": COCITED_MIN}))
    rows = [
        ReferenceRow(**{key: value for key, value in row._mapping.items() if key not in ("note_similarity", "citing")})
        for row in raw
    ]
    citing_by_id = {row.id: row.citing for row in raw}
    to_read = sorted((r for r in rows if r.queued_at is not None), key=lambda r: (-r.queued_at.timestamp(), r.id))
    cited_by_several = [r for r in rows if r.cocitation >= COCITED_MIN]
    citing_several = sorted(
        (dataclasses.replace(r, cocitation=citing_by_id[r.id]) for r in rows if citing_by_id[r.id] >= COCITED_MIN),
        key=lambda r: (-(r.year or -1), -r.cocitation, r.id),
    )
    coverage_rows = list(
        await session.execute(
            text(
                """
                SELECT p.id, p.title, p.references_state, p.references_error, p.references_requested_at,
                       p.references_state = 'ready' OR EXISTS (
                           SELECT 1 FROM paper_references pr WHERE pr.paper_id = p.id
                       ) AS fetched
                  FROM papers p
                 WHERE CAST(:workspace AS uuid) IS NULL
                    OR EXISTS (
                        SELECT 1 FROM workspace_papers wp WHERE wp.paper_id = p.id AND wp.workspace_id = :workspace
                    )
                """
            ),
            {"workspace": workspace_id},
        )
    )
    unfetched = sorted(
        (row for row in coverage_rows if not row.fetched),
        key=lambda row: (row.title, row.id),
    )
    coverage = Coverage(
        fetched=sum(1 for row in coverage_rows if row.fetched),
        total=len(coverage_rows),
        unfetched=[
            UnfetchedPaper(
                row.id, row.title, *shown_state(row.references_state, row.references_error, row.references_requested_at)
            )
            for row in unfetched
        ],
    )
    return ReferencePage(
        coverage=coverage, to_read=to_read, cited_by_several=cited_by_several, citing_several=citing_several
    )


# --- import ------------------------------------------------------------------------------------------------------


async def import_reference(
    session: AsyncSession, providers: discovery.Providers, ref_id: uuid.UUID, pdf_dir: Path
) -> Paper:
    """Downloads the reference's first free PDF and creates the paper, as Find papers' Add does (D84). The caller
    enqueues ingest. The new paper's own references are not fetched (P5)."""
    ref = await session.get(ExternalRef, ref_id)
    if ref is None:
        raise NotFound(f"reference {ref_id} not found")
    if ref.imported_as is not None:
        raise Conflict(discovery.ALREADY_IN_LIBRARY)
    candidate = Candidate(
        title=ref.title, authors=ref.authors, year=ref.year, venue=ref.venue, doi=ref.doi,
        external_ids=dict(ref.external_ids), cited_by_count=ref.cited_by_count, pdf_urls=ref.pdf_urls,
    )  # fmt: skip
    paper = await discovery.add(session, providers, candidate, pdf_dir)
    # Only doi/arxiv matter for this match, same as before — not every identifier, deliberately.
    arxiv_only = {"arxiv": ref.external_ids["arxiv"]} if "arxiv" in ref.external_ids else {}
    same_paper = [ExternalRef.id == ref_id, *_same_reference(arxiv_only, ref.doi)]
    # Dealt with: deleting the paper later won't put it back in To read (D164).
    await session.execute(update(ExternalRef).where(or_(*same_paper)).values(imported_as=paper.id, queued_at=None))
    await session.commit()
    return paper
