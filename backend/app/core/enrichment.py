"""Paper metadata from three sources, in rising precedence: the PDF, OpenAlex, the user.

- Never fatal: a paper with no OpenAlex match, or with OpenAlex unreachable, stays fully usable.
- A stored OpenAlex ID, a DOI or an arXiv ID is trusted. A title-search hit is accepted only when its year (±1) and
  its first author agree with the PDF.
- Authors are keyed on OpenAlex IDs, never on names. Author details are refetched only after 30 days.
- A field the user corrected (papers.manual_fields) is never overwritten by the PDF or by OpenAlex.
"""

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import delete, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, InvalidInput
from app.core.papers import get_paper
from app.core.source_types import ARXIV_DOI_PREFIX, peer_reviewed_of, source_type_of
from app.models import Author, Paper, paper_authors, paper_topics
from app.providers import crossref, openalex

logger = logging.getLogger(__name__)

AUTHOR_REFRESH_AFTER = timedelta(days=30)
YEAR_TOLERANCE = 1  # an arXiv preprint and its published version are often dated a year apart

_DOI = re.compile(r"10\.\d{4,9}/\S+")
# New-style ("1810.04805") or old-style, pre-2007 ("hep-th/9901001", "math.GT/0309136") IDs, an optional "vN" dropped.
_ARXIV_ID = re.compile(r"arXiv:(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?", re.IGNORECASE)
_PDF_DATE_YEAR = re.compile(r"D:(\d{4})")
# ponytail: splits "A; B and C" and "A, B". A "Family, Given" author field becomes two names; the user can correct it.
_AUTHOR_SEPARATORS = re.compile(r"\s*(?:;|,|\band\b)\s*")
_KEYWORD_SEPARATORS = re.compile(r"\s*[;,]\s*")


@dataclass(frozen=True)
class PdfHints:
    doi: str | None
    arxiv_id: str | None
    years: frozenset[int]  # years the PDF suggests: its arXiv ID's year and its creation date's year
    text: str  # the embedded author field and page 1's text, where a match's first author must appear
    authors: list[str]  # the embedded author field, split into names
    keywords: list[str]  # the embedded keywords: the author's own topic labels


def normalize_doi(value: str) -> str | None:
    """The first DOI in `value`, lowercased (DOIs are case-insensitive), or None."""
    match = _DOI.search(value)
    if match is None:
        return None
    doi = match.group().lower()
    while True:
        trimmed = doi.rstrip(".,;")
        # A DOI may contain balanced brackets ("s0140-6736(20)30367-6"); only an unbalanced closing one is prose.
        for open_ch, close_ch in ("()", "[]"):
            if trimmed.endswith(close_ch) and trimmed.count(close_ch) > trimmed.count(open_ch):
                trimmed = trimmed[:-1]
        if trimmed == doi:
            return trimmed
        doi = trimmed


def _split(value: str, separators: re.Pattern) -> list[str]:
    return [part for part in separators.split(value) if part]


def pdf_hints(first_page_text: str, metadata: dict[str, str]) -> PdfHints:
    arxiv = _ARXIV_ID.search(first_page_text)
    created = _PDF_DATE_YEAR.match(metadata.get("creationDate", ""))
    years = {int(created.group(1))} if created else set()
    if arxiv and "/" not in arxiv.group(1):
        years.add(2000 + int(arxiv.group(1)[:2]))  # new-style arXiv IDs start with yymm
    # ponytail: old-style IDs (archive/YYMMNNN) span 1991-2007, so a bare "2000 + yy" would misread them; skip.
    author = metadata.get("author", "")
    return PdfHints(
        doi=normalize_doi(" ".join([metadata.get("subject", ""), metadata.get("keywords", ""), first_page_text])),
        arxiv_id=arxiv.group(1) if arxiv else None,
        years=frozenset(years),
        text=f"{author}\n{first_page_text}",
        authors=_split(author, _AUTHOR_SEPARATORS),
        keywords=_split(metadata.get("keywords", ""), _KEYWORD_SEPARATORS),
    )


_UNICODE_HYPHENS = str.maketrans("‐‑", "--")  # OpenAlex writes "Ming‐Wei" with U+2010


def _fold(text: str) -> str:
    """Casefolded, without accents, ASCII hyphens: "Chabrière‐Smith" and "CHABRIERE-SMITH" compare equal."""
    text = unicodedata.normalize("NFKD", text).translate(_UNICODE_HYPHENS)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def _first_author_matches(work: dict[str, Any], hints: PdfHints) -> bool:
    authorships = work.get("authorships") or []
    names = _fold(authorships[0]["author"]["display_name"]).split() if authorships else []
    # ponytail: the family name as a whole word on page 1. A name in a non-Latin script never matches: a miss,
    # never a wrong match.
    return bool(names) and re.search(rf"\b{re.escape(names[-1])}\b", _fold(hints.text)) is not None


def is_confirmed_match(work: dict[str, Any], hints: PdfHints) -> bool:
    """A title-search hit counts only when its year and its first author agree with the PDF."""
    year = work.get("publication_year")
    if year is None or not any(abs(year - y) <= YEAR_TOLERANCE for y in hints.years):
        return False
    return _first_author_matches(work, hints)


def short_id(url: str | None) -> str | None:
    """ "https://openalex.org/W2963341956" -> "W2963341956"; also strips "https://orcid.org/"."""
    return url.rsplit("/", 1)[-1] if url else None


def abstract_text(inverted_index: dict[str, list[int]] | None) -> str | None:
    if not inverted_index:
        return None
    return " ".join(word for _, word in sorted((i, word) for word, places in inverted_index.items() for i in places))


def work_fields(work: dict[str, Any]) -> dict[str, Any]:
    """The papers columns an OpenAlex work fills."""
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    best_oa = work.get("best_oa_location") or {}
    fields = {
        "openalex_id": short_id(work["id"]),
        "doi": normalize_doi(work.get("doi") or ""),
        "title": work.get("title"),
        "abstract": abstract_text(work.get("abstract_inverted_index")),
        "authors": [a["author"]["display_name"] for a in work.get("authorships") or []],
        "year": work.get("publication_year"),
        "venue": source.get("display_name") or location.get("raw_source_name"),
        "issn": source.get("issn_l"),
        "type": work.get("type"),
        "is_retracted": bool(work.get("is_retracted")),
        "oa_status": (work.get("open_access") or {}).get("oa_status"),
        "oa_url": best_oa.get("pdf_url") or best_oa.get("landing_page_url"),
        "cited_by_count": work.get("cited_by_count"),
        "referenced_works_count": work.get("referenced_works_count"),
    }
    return fields


def work_topics(work: dict[str, Any]) -> dict[str, float]:
    """OpenAlex topics, keywords and concepts by label. A label in more than one list keeps its best score."""
    scores: dict[str, float] = {}
    for item in [*(work.get("topics") or []), *(work.get("keywords") or []), *(work.get("concepts") or [])]:
        scores[item["display_name"]] = max(scores.get(item["display_name"], 0.0), item["score"])
    return scores


async def find_work(http: httpx.AsyncClient, paper: Paper, hints: PdfHints) -> dict[str, Any] | None:
    """The stored openalex_id and a manually corrected DOI are trusted outright: the user (or an earlier confirmed
    match) vouched for them. A DOI merely hinted by the PDF -- or left over from an earlier unconfirmed fallback
    write -- and an arXiv ID from the PDF must also pass the first-author check; a miss falls through to title
    search. Raises httpx.HTTPError when OpenAlex fails.
    """
    trusted_doi = paper.doi if paper.doi and "doi" in paper.manual_fields else None
    for key in filter(None, [paper.openalex_id, trusted_doi and f"doi:{trusted_doi}"]):
        if work := await openalex.get_work(http, key):
            return work

    hinted_doi = None if trusted_doi else (paper.doi or hints.doi)
    hinted_keys = [hinted_doi and f"doi:{hinted_doi}", hints.arxiv_id and f"doi:{ARXIV_DOI_PREFIX}{hints.arxiv_id}"]
    for key in filter(None, hinted_keys):
        if (work := await openalex.get_work(http, key)) and _first_author_matches(work, hints):
            return work

    if not hints.years:
        return None  # a hit could never be confirmed, so don't spend 10 credits on the search
    return next((w for w in await openalex.search_works(http, paper.title) if is_confirmed_match(w, hints)), None)


async def _replace_topics(session: AsyncSession, paper_id: uuid.UUID, source: str, scores: dict) -> None:
    topics = paper_topics.c
    await session.execute(delete(paper_topics).where(topics.paper_id == paper_id, topics.source == source))
    if scores:
        rows = [{"paper_id": paper_id, "source": source, "label": label, "score": s} for label, s in scores.items()]
        await session.execute(insert(paper_topics), rows)


def _regresses(new: Any, old: Any) -> bool:
    """True when a match or the PDF fallback would replace an already-known value with a blank."""
    return new in (None, []) and old not in (None, [])


async def _taken(session: AsyncSession, paper_id: uuid.UUID, column: Any, value: Any) -> bool:
    """True when a different paper already holds this UNIQUE `column` value."""
    # ponytail: check-then-UPDATE, not atomic. ARQ runs up to 10 jobs at once, so two concurrent duplicates could
    # still both pass this check and hit the UNIQUE constraint on the later UPDATE; the worker's `_enrich` catch-all
    # logs it and the paper still ends `ready`. Upgrade path: `ON CONFLICT` on the UPDATE, or a savepoint around
    # check+write.
    return bool(await session.scalar(select(exists().where(column == value, Paper.id != paper_id))))


async def _replace_authorships(session: AsyncSession, paper_id: uuid.UUID, work: dict[str, Any]) -> list[str]:
    """Upserts the work's authors by OpenAlex ID and links them in author order. Returns their OpenAlex IDs."""
    first_listing: dict[str, tuple[int, dict]] = {}
    for position, authorship in enumerate(work.get("authorships") or [], start=1):
        if openalex_id := short_id(authorship["author"].get("id")):  # never create an author from a name alone
            first_listing.setdefault(openalex_id, (position, authorship))
    await session.execute(delete(paper_authors).where(paper_authors.c.paper_id == paper_id))
    if not first_listing:
        return []

    new_authors = [
        {"openalex_id": oid, "display_name": a["author"]["display_name"], "orcid": short_id(a["author"].get("orcid"))}
        for oid, (_, a) in first_listing.items()
    ]
    # ponytail: two OpenAlex IDs sharing one ORCID break authors.orcid UNIQUE, and the paper keeps no authors (still
    # non-fatal). Drop the ORCID on conflict if OpenAlex's duplicate profiles ever show up.
    await session.execute(pg_insert(Author).values(new_authors).on_conflict_do_nothing(index_elements=["openalex_id"]))
    rows = await session.execute(select(Author.openalex_id, Author.id).where(Author.openalex_id.in_(first_listing)))
    author_ids = dict(rows.all())
    links = [
        {
            "paper_id": paper_id,
            "author_id": author_ids[oid],
            "position": position,
            "is_corresponding": bool(a.get("is_corresponding")),
            # The affiliation printed on this paper, not the author's current one.
            "institution": next((i["display_name"] for i in a.get("institutions") or []), None),
        }
        for oid, (position, a) in first_listing.items()
    ]
    await session.execute(insert(paper_authors), links)
    return list(first_listing)


async def refresh_authors(session: AsyncSession, http: httpx.AsyncClient, openalex_ids: list[str]) -> None:
    """Fetches details (h-index, works count, ...) for authors not fetched in the last 30 days, 50 per request."""
    stale = list(
        await session.scalars(
            select(Author.openalex_id).where(
                Author.openalex_id.in_(openalex_ids),
                or_(Author.fetched_at.is_(None), Author.fetched_at < func.now() - AUTHOR_REFRESH_AFTER),
            )
        )
    )
    if not stale:
        return
    stale.sort(key=openalex_ids.index)  # author order, so requests are predictable
    for record in await openalex.get_authors(http, stale):
        values = {
            "display_name": record["display_name"],
            "orcid": short_id(record.get("orcid")),
            "alt_names": record.get("display_name_alternatives") or [],
            "last_institution": next((i["display_name"] for i in record.get("last_known_institutions") or []), None),
            "works_count": record.get("works_count"),
            "cited_by_count": record.get("cited_by_count"),
            "h_index": (record.get("summary_stats") or {}).get("h_index"),
            "topics": [{"label": t["display_name"], "count": t.get("count")} for t in record.get("topics") or []],
            "fetched_at": func.now(),
        }
        await session.execute(update(Author).where(Author.openalex_id == short_id(record["id"])).values(**values))
    await session.commit()


async def _write(session: AsyncSession, paper: Paper, fields: dict[str, Any]) -> None:
    # The OpenAlex lookup can take seconds; re-read the lock right before writing, in case a PATCH landed meanwhile.
    await session.refresh(paper, ["manual_fields"])
    values = {k: v for k, v in fields.items() if k not in paper.manual_fields and not _regresses(v, getattr(paper, k))}
    if "source_type" in paper.manual_fields:  # peer_reviewed was derived from a type the owner replaced
        values.pop("peer_reviewed", None)
    # A duplicate upload can match the very work (or carry the very DOI) an existing paper already holds; doi and
    # openalex_id are both UNIQUE, so a value another paper already has is dropped here instead of raising. The
    # copy still gets the match's authorships and topics: those are written regardless, keyed on paper_id, not on
    # the papers.openalex_id column this function may have just dropped.
    for column, key in ((Paper.doi, "doi"), (Paper.openalex_id, "openalex_id")):
        if (value := values.get(key)) and await _taken(session, paper.id, column, value):
            values = {k: v for k, v in values.items() if k != key}
    if values:
        await session.execute(update(Paper).where(Paper.id == paper.id).values(**values))


async def fill_source_type(
    session: AsyncSession, paper_id: uuid.UUID, raw_type: str | None, crossref_http: httpx.AsyncClient | None
) -> bool:
    """Sets source_type, peer_reviewed and published_doi (never past the owner's lock). `raw_type` is OpenAlex's type;
    without one, a non-arXiv DOI is asked of Crossref (when `crossref_http` is given). A paper that already has a
    source type is not asked again. Crossref errors are logged, never raised. True when Crossref was asked."""
    paper = await get_paper(session, paper_id)
    await session.refresh(paper)
    doi = normalize_doi(paper.doi or "")
    fields: dict[str, Any] = {}
    asked = False
    if (source_type := source_type_of(raw_type, doi)) is None and paper.source_type is None and doi and crossref_http:
        asked = True
        try:
            work = await crossref.get_work(crossref_http, doi)
        except httpx.HTTPError as exc:
            logger.warning("Crossref lookup failed for paper %s, source type left as is: %r", paper_id, exc)
            work = None
        if work:
            source_type = source_type_of(work.get("type"), doi, crossref=True)
            preprint_of = ((work.get("relation") or {}).get("is-preprint-of") or [{}])[0].get("id")
            fields["published_doi"] = normalize_doi(preprint_of or "")
    if source_type:
        fields |= {"source_type": source_type, "peer_reviewed": peer_reviewed_of(source_type)}
    if fields:
        await _write(session, paper, fields)
        await session.commit()
    return asked


async def enrich_paper(
    session: AsyncSession,
    http: httpx.AsyncClient | None,
    paper_id: uuid.UUID,
    hints: PdfHints,
    crossref_http: httpx.AsyncClient | None = None,
) -> None:
    """Fills a paper's metadata from OpenAlex, or from the PDF when OpenAlex has nothing. No client: OpenAlex is off.

    OpenAlex failures are logged and fall back to the PDF. A DOI or `openalex_id` another paper already holds is
    silently dropped instead of raising (see `_write`).
    """
    paper = await get_paper(session, paper_id)
    await session.refresh(paper)  # the pipeline changed the title with a bulk UPDATE since this object was loaded
    try:
        work = await find_work(http, paper, hints) if http else None
    except httpx.HTTPError as exc:
        logger.warning("OpenAlex lookup failed for paper %s, using PDF metadata: %r", paper_id, exc)
        work = None

    await _replace_topics(session, paper_id, "author", dict.fromkeys(hints.keywords))
    if work is None:
        if paper.openalex_id is None:
            fallback = {k: v for k, v in {"authors": hints.authors, "doi": hints.doi}.items() if v}
            if fallback:
                await _write(session, paper, fallback)  # never replaces what an earlier match found
        await session.commit()
        await fill_source_type(session, paper_id, None, crossref_http)
        return

    await _write(session, paper, work_fields(work))
    await _replace_topics(session, paper_id, "openalex", work_topics(work))
    await session.commit()
    await fill_source_type(session, paper_id, work.get("type"), crossref_http)
    author_ids = await _replace_authorships(session, paper_id, work)
    await session.commit()
    try:
        await refresh_authors(session, http, author_ids)
    except httpx.HTTPError as exc:  # the paper and its authorships are saved; details come on the next run
        logger.warning("OpenAlex author fetch failed for paper %s: %r", paper_id, exc)


async def correct_metadata(session: AsyncSession, paper_id: uuid.UUID, fields: dict[str, Any]) -> Paper:
    """The user's corrections, written as given and added to manual_fields so nothing overwrites them later.

    `fields` holds only what the user changed. Raises NotFound, InvalidInput (a DOI that isn't one) or
    Conflict("doi_taken").
    """
    paper = await get_paper(session, paper_id)
    clears_match = {}
    if fields.get("doi") is not None:
        doi = normalize_doi(fields["doi"])
        if doi is None:
            raise InvalidInput(f"not a DOI: {fields['doi']!r}")
        if await session.scalar(select(Paper.id).where(Paper.doi == doi, Paper.id != paper_id)):
            raise Conflict("doi_taken")
        fields = {**fields, "doi": doi}
        if doi != paper.doi:
            # A different DOI can only mean the stored match (if any) is for the wrong paper. Not locked in
            # manual_fields: enrichment stays free to write back whatever the corrected DOI actually resolves to.
            clears_match = {"openalex_id": None}
    manual_fields = sorted(set(paper.manual_fields) | fields.keys())
    await session.execute(
        update(Paper).where(Paper.id == paper_id).values(**fields, **clears_match, manual_fields=manual_fields)
    )
    await session.commit()
    await session.refresh(paper)
    return paper
