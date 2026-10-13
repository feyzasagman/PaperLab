"""Find papers outside the library and add a free copy (M19, M19.5).

- A search asks every source that is on and answers that kind of query, at once, and merges what they find (D72,
  D73). Semantic Scholar then adds arXiv IDs and free PDF links (D70), and Unpaywall adds PDF links for DOIs still
  without one. Similar papers come from Semantic Scholar (D67).
- A source that fails is a notice; the search fails only when every source it asked failed (P7).
- Nothing found is stored: a candidate is in the library when its OpenAlex ID or DOI matches a paper (D66).
- A download counts only when the body starts with %PDF. No paywall workaround (D68).
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import paper_sources, papers, source_registry
from app.core.candidates import (
    ARXIV_ID,
    Candidate,
    arxiv_from_doi,
    from_s2,
    merge,
    normal_title,
    ordered_pdf_urls,
    with_s2,
)
from app.core.enrichment import ARXIV_DOI_PREFIX
from app.core.errors import Conflict
from app.core.paper_sources import KEYED, NAMES, SourceSettings
from app.core.source_registry import ASKS
from app.models import Paper
from app.providers import discovery_fake, semantic_scholar, unpaywall

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 20
SIMILAR_LIMIT = 10
UNPAYWALL_CONCURRENCY = 5
MAX_PDF_BYTES = 100 * 1024 * 1024
PDF_TIMEOUT = httpx.Timeout(10.0, read=30.0)

NO_SOURCE = "No paper source that can look this up is on. Turn one on in Settings → Paper sources."
SOURCE_BUSY = "{name} is busy or unreachable."
KEY_REFUSED = "{name} refused its API key. Check it in Settings → Paper sources."
S2_OFF = "Semantic Scholar is off. Turn it on in Settings → Paper sources to see similar papers."
S2_BUSY = "Semantic Scholar is busy or unreachable. Try again in a minute."
S2_UNKNOWN = "Semantic Scholar doesn't know this paper, so it has no suggestions."
ALREADY_IN_LIBRARY = "This paper is already in your library."
NO_FREE_PDF = "No free PDF was found for this paper. Open its page, download the PDF and use Upload PDFs."

_ARXIV_QUERY = re.compile(
    rf"^(?:arxiv:|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)?({ARXIV_ID})(?:v\d+)?(?:\.pdf)?$", re.IGNORECASE
)
_DOI = re.compile(r"\b(10\.\d{4,9}/\S+)")
_OPENALEX_QUERY = re.compile(r"^(?:https?://(?:api\.)?openalex\.org/(?:works/)?)?(W\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Providers:
    """The clients one request uses. A source that is off, or lacks a key it needs, has none in `clients`; a PDF
    host (arxiv.org, a publisher, a repository, or a redirect target) never agreed to receive the owner's email,
    so `pdf` is separate and always present."""

    pdf: httpx.AsyncClient
    clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)

    def client(self, source: str) -> httpx.AsyncClient | None:
        return self.clients.get(source)

    def without(self, *sources: str) -> "Providers":
        """A copy with these sources' clients removed — for tests simulating a source being off."""
        return replace(self, clients={k: v for k, v in self.clients.items() if k not in sources})

    async def aclose(self) -> None:
        await self.pdf.aclose()
        for client in self.clients.values():
            await client.aclose()


@dataclass(frozen=True)
class SearchResult:
    results: list[Candidate]
    notices: list[str]  # one per source that failed


def build_providers(sources: SourceSettings, transport: httpx.AsyncBaseTransport | None = None) -> Providers:
    on, email, keys = sources.enabled, sources.contact_email, sources.api_keys
    clients = {
        spec.id: spec.new_client(email=email, api_key=keys.get(spec.key_source or spec.id), transport=transport)
        for spec in source_registry.REGISTRY
        if (sources.unpaywall_on if spec.id == "unpaywall" else on[spec.id])
    }
    return Providers(
        # No email: unlike the sources, a PDF host (arxiv.org, a publisher, a repository, or a redirect target) never
        # agreed to receive the owner's email.
        pdf=httpx.AsyncClient(
            timeout=PDF_TIMEOUT, follow_redirects=True, headers={"User-Agent": "PaperLab"}, transport=transport
        ),
        clients=clients,
    )


@asynccontextmanager
async def open_providers(session: AsyncSession) -> AsyncIterator[Providers]:
    """The clients for one request or tool call, from Settings → Paper sources, closed after it. With
    DISCOVERY_PROVIDER=fake every source answers offline (E2E)."""
    sources = await paper_sources.get(session)
    transport = None
    if settings.discovery_provider == "fake":
        transport = discovery_fake.transport()
        # The fake answers Unpaywall offline, so its placeholder can stand in for the email Unpaywall needs.
        sources = replace(sources, contact_email=sources.contact_email or discovery_fake.MAILTO)
    providers = build_providers(sources, transport)
    try:
        yield providers
    finally:
        await providers.aclose()


def classify_query(query: str) -> tuple[str, str]:
    """("doi" | "arxiv" | "openalex" | "title", value). An arXiv DOI counts as its arXiv ID."""
    text = " ".join(query.split())
    if found := _ARXIV_QUERY.match(text):
        return "arxiv", found.group(1).lower()
    if work := _OPENALEX_QUERY.match(text):
        return "openalex", work.group(1).upper()
    if doi := _DOI.search(text):
        value = doi.group(1).rstrip(".,;)").lower()
        if value.startswith(ARXIV_DOI_PREFIX):
            return "arxiv", value.removeprefix(ARXIV_DOI_PREFIX)
        return "doi", value
    return "title", text



def notice(source: str, error: httpx.HTTPError) -> str:
    # A source that takes no key (e.g. Crossref) can't have refused one, whatever status it answered with.
    refused = (
        source in KEYED and isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403)
    )
    return (KEY_REFUSED if refused else SOURCE_BUSY).format(name=NAMES[source])


def _library_dois(candidate: Candidate) -> list[str]:
    """Lowercase: a candidate the API received may spell its DOI in any case."""
    arxiv_id = candidate.external_ids.get("arxiv")
    arxiv_doi = arxiv_id and f"{ARXIV_DOI_PREFIX}{arxiv_id}"
    return [doi.lower() for doi in (candidate.doi, arxiv_doi) if doi]


async def mark_in_library(session: AsyncSession, candidates: list[Candidate]) -> list[Candidate]:
    """New candidates with `paper_id` set where the library holds the paper, by OpenAlex ID or DOI (any case)."""
    ids = [oa for c in candidates if (oa := c.external_ids.get("openalex"))]
    dois = [doi for c in candidates for doi in _library_dois(c)]
    rows = (
        await session.execute(
            select(Paper.id, func.lower(Paper.doi), Paper.openalex_id).where(
                or_(Paper.openalex_id.in_(ids), func.lower(Paper.doi).in_(dois))
            )
        )
    ).all()
    library = {key: paper_id for paper_id, doi, openalex_id in rows for key in (doi, openalex_id) if key}
    return [
        replace(
            c,
            paper_id=next(
                (library[k] for k in (c.external_ids.get("openalex"), *_library_dois(c)) if k in library), None
            ),
        )
        for c in candidates
    ]


async def search(session: AsyncSession, providers: Providers, query: str) -> SearchResult:
    kind, value = classify_query(query)
    asks = {source: ask for source, ask in ASKS[kind].items() if providers.client(source) is not None}
    if not asks:
        raise Conflict(NO_SOURCE)
    answers = await asyncio.gather(
        *(ask(providers.client(source), kind, value) for source, ask in asks.items()), return_exceptions=True
    )
    found: dict[str, list[Candidate]] = {}
    notices: list[str] = []
    for source, answer in zip(asks, answers):
        if isinstance(answer, httpx.HTTPError):
            logger.info("%s failed for this search: %s", source, answer)
            notices.append(notice(source, answer))
        elif isinstance(answer, BaseException):
            raise answer
        else:
            found[source] = answer
    if not found:
        raise Conflict(" ".join(notices))
    candidates = merge(found, SEARCH_LIMIT)
    if (s2 := providers.client("semantic_scholar")) is not None:
        candidates = await _add_s2_links(s2, candidates)
    candidates = await _add_unpaywall_links(providers.client("unpaywall"), candidates)
    return SearchResult(await mark_in_library(session, candidates), notices)


async def _add_s2_links(http: httpx.AsyncClient, candidates: list[Candidate]) -> list[Candidate]:
    """One batch request for the candidates Semantic Scholar didn't find itself. Never fatal: when it fails, the
    results show as they are."""
    with_doi = [c for c in candidates if c.doi and not c.external_ids.get("semantic_scholar")]
    try:
        found = await semantic_scholar.get_papers(http, [f"DOI:{c.doi}" for c in with_doi])
    except httpx.HTTPError as exc:
        logger.info("no Semantic Scholar links for this search: %s", exc)
        return candidates
    by_doi = {c.doi: paper for c, paper in zip(with_doi, found)}
    return [with_s2(c, by_doi.get(c.doi)) for c in candidates]


async def _add_unpaywall_links(http: httpx.AsyncClient | None, candidates: list[Candidate]) -> list[Candidate]:
    """Free PDF links for candidates with a DOI and none yet: one request each, so only where a badge can change
    (D72). Never fatal: a failed lookup leaves its candidate as it is."""
    if http is None:
        return candidates
    limit = asyncio.Semaphore(UNPAYWALL_CONCURRENCY)
    unreachable = False  # set on the first connect error or timeout; a 4xx/5xx for one DOI doesn't set it

    async def with_links(candidate: Candidate) -> Candidate:
        nonlocal unreachable
        if not candidate.doi or candidate.pdf_urls or unreachable:
            return candidate
        try:
            async with limit:
                if unreachable:
                    return candidate
                urls = await unpaywall.pdf_urls(http, candidate.doi)
        except httpx.TransportError as exc:
            logger.info("Unpaywall is unreachable, skipping the rest of this search: %s", exc)
            unreachable = True
            return candidate
        except httpx.HTTPError as exc:
            logger.info("no Unpaywall links for %s: %s", candidate.doi, exc)
            return candidate
        return replace(candidate, pdf_urls=ordered_pdf_urls(candidate.external_ids.get("arxiv"), *urls))

    return list(await asyncio.gather(*map(with_links, candidates)))


def _same_paper_keys(title: str, *ids: str | None) -> set[str]:
    return {i.lower() for i in ids if i} | {normal_title(title)}


async def s2_key(http: httpx.AsyncClient, doi: str | None, title: str) -> str | None:
    """The key Semantic Scholar knows something by: `arXiv:<id>` for an arXiv DOI, `DOI:<doi>`, else its best
    title match (one request). None when it has no match. Raises httpx.HTTPError. Takes doi/title directly (not a
    Paper) so it works for anything with these two fields — an imported Paper or a not-yet-imported hit's own
    ExternalRef alike (snowball's own use, workspace_search.py)."""
    doi = (doi or "").lower()
    if arxiv_id := arxiv_from_doi(doi):
        return f"arXiv:{arxiv_id}"
    if doi:
        return f"DOI:{doi}"
    return await semantic_scholar.match_title(http, title)


async def similar(
    session: AsyncSession, providers: Providers, paper: Paper, limit: int = SIMILAR_LIMIT
) -> list[Candidate]:
    if (s2 := providers.client("semantic_scholar")) is None:
        raise Conflict(S2_OFF)
    doi = (paper.doi or "").lower()
    arxiv_id = arxiv_from_doi(doi)
    try:
        key = await s2_key(s2, paper.doi, paper.title)
        found = await semantic_scholar.recommend(s2, key, limit + 1) if key else None
    except httpx.HTTPError as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
            raise Conflict(KEY_REFUSED.format(name=NAMES["semantic_scholar"])) from exc
        raise Conflict(S2_BUSY) from exc
    if found is None:
        raise Conflict(S2_UNKNOWN)
    own = _same_paper_keys(paper.title, doi, arxiv_id, paper.openalex_id)
    candidates = [
        c for c in map(from_s2, found) if not own & _same_paper_keys(c.title, c.doi, c.external_ids.get("arxiv"))
    ]
    return await mark_in_library(session, await _add_unpaywall_links(providers.client("unpaywall"), candidates[:limit]))


async def _fetch_pdf(http: httpx.AsyncClient, url: str) -> bytes | None:
    async with http.stream("GET", url) as response:
        if response.status_code != 200:
            logger.info("no PDF at %s: HTTP %d", url, response.status_code)
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_PDF_BYTES:
                logger.info("no PDF at %s: over %d bytes", url, MAX_PDF_BYTES)
                return None
    if not body.startswith(papers.PDF_MAGIC):
        logger.info("no PDF at %s: the body is not a PDF", url)
        return None
    return bytes(body)


async def download_pdf(http: httpx.AsyncClient, urls: list[str]) -> bytes | None:
    """The first URL whose body is a PDF, in order. A failing URL moves on to the next."""
    for url in urls:
        try:
            if (data := await _fetch_pdf(http, url)) is not None:
                return data
        except httpx.HTTPError as exc:
            logger.info("no PDF at %s: %s", url, exc)
    return None


def _prefill(candidate: Candidate) -> dict[str, Any]:
    """Enrichment trusts a stored openalex_id, and a DOI only when it's locked (D69). The title is always locked:
    ingest's PDF-font heuristic and enrichment's metadata are both worse than the title the source already gave us,
    so it must survive both (M7.5)."""
    doi = next(iter(_library_dois(candidate)), None)
    openalex_id = candidate.external_ids.get("openalex")
    fields = {
        "title": candidate.title,
        "authors": candidate.authors,
        "year": candidate.year,
        "venue": candidate.venue,
        "cited_by_count": candidate.cited_by_count,
        "doi": doi,
        "openalex_id": openalex_id,
    }
    locked = ["doi", "title"] if doi and not openalex_id else ["title"]
    return fields | {"manual_fields": locked}


def _filename(title: str) -> str:
    return f"{re.sub(r'[^\w\- ]+', '', title).strip()[:80] or 'paper'}.pdf"


async def add(session: AsyncSession, providers: Providers, candidate: Candidate, pdf_dir: Path) -> Paper:
    """Downloads the candidate's first free PDF and creates the paper. The caller enqueues ingest."""
    [marked] = await mark_in_library(session, [candidate])
    if marked.paper_id is not None:
        raise Conflict(ALREADY_IN_LIBRARY)
    # Release the connection (and its transaction) before a download that can take up to several 30s reads.
    await session.commit()
    data = await download_pdf(providers.pdf, candidate.pdf_urls)
    if data is None:
        raise Conflict(NO_FREE_PDF)
    # ponytail: two adds of one paper racing past the check above hit papers' UNIQUE doi and 500. The buttons disable
    # while adding; catch IntegrityError here if it ever happens.
    return await papers.create_paper(session, _filename(candidate.title), data, pdf_dir, prefill=_prefill(candidate))
