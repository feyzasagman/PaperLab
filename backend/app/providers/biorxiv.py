"""bioRxiv and medRxiv, harvested via their shared real API (https://api.biorxiv.org) -- one host, the
two platforms differ only by the `server` path segment ("biorxiv" or "medrxiv"). Network failures,
timeouts and error statuses (429 included) raise httpx.HTTPError.

Probed live on 2026-10-10: there is no free-text search endpoint at all -- the only API is a
chronological date-range listing, GET /details/<server>/<from>/<until>/<cursor>, 30 records per page,
cursor a plain integer offset (not a resumption token -- confirmed live: requesting cursor=30 after a
30-record page correctly returns the next 30). A real response's own `messages[0]` carries `status`
("ok" for a normal page), `total` (the full count for the whole date range, as a STRING -- confirmed
live, must be int()-converted explicitly or arithmetic against it raises TypeError) and `count`. A date
range with genuinely zero matching records, OR a cursor requested past the real total, both answer
`{"status": "no posts found"}` with an empty `collection` -- confirmed live on both a far-future date
range and a past-the-end cursor -- a real, legitimate "nothing here" signal, not a failure. Any OTHER
non-"ok" status raises, the same defensive stance Batch 6A's own OAI-PMH <error> handling takes for
IACR ePrint. Each real record carries its own DOI (confirmed live, present on every sampled record,
resolving cleanly via https://doi.org/<doi>) and its own real publication `date` (confirmed live, unlike
IACR's OAI datestamp, this is the preprint's own posting date, not a last-modified time). No key, no
documented rate limit.
"""

import httpx
from sqlalchemy import and_, or_, select

from app.db import SessionLocal
from app.models import ExternalRef
from app.providers.arxiv import title_words

BASE_URL = "https://api.biorxiv.org"
TIMEOUT = httpx.Timeout(30.0)

# Confirmed live on 2026-10-10: api.biorxiv.org has no Cloudflare or similar bot-protection (plain Apache,
# httpx's own default User-Agent gets a clean 200) -- unlike IACR ePrint (Batch 6A). Setting an honest,
# identifying header anyway: it costs nothing and is standard API-citizen etiquette regardless of whether
# this particular host currently requires it.
_USER_AGENT = "PaperLab/1.0 (+https://github.com/khannoussi-malek/PaperLab; bioRxiv/medRxiv API harvester)"


def new_client(
    *, email: str | None = None, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    # Identical for both servers -- the host, headers and timeout never depend on which server a later
    # call targets, so one function serves both bioRxiv's and medRxiv's own SourceSpec entries directly.
    return httpx.AsyncClient(
        base_url=BASE_URL, timeout=TIMEOUT, transport=transport, headers={"User-Agent": _USER_AGENT}
    )


def _checked(response: httpx.Response) -> httpx.Response:
    """Like response.raise_for_status(), but the message never embeds the request URL -- same convention
    every provider in this codebase follows."""
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"bioRxiv/medRxiv API answered HTTP {response.status_code}", request=response.request, response=response
        )
    return response


async def fetch_page(
    http: httpx.AsyncClient, server: str, *, from_date: str, until_date: str, cursor: int = 0
) -> tuple[list[dict], int | None]:
    """One page of real API results for `server` ("biorxiv" or "medrxiv"). Returns (entries,
    next_cursor_or_None) -- entries is the raw `collection` list (each a dict with title/authors/doi/
    date/abstract/etc., straight from the real API, consumed by candidates.py's own from_biorxiv_entry).
    """
    response = await http.get(f"/details/{server}/{from_date}/{until_date}/{cursor}")
    body = _checked(response).json()
    message = body["messages"][0]
    if message["status"] != "ok":
        if message["status"] == "no posts found":
            return [], None
        raise httpx.HTTPStatusError(
            f"bioRxiv/medRxiv API error: {message['status']}", request=response.request, response=response
        )
    entries = body["collection"]
    total = int(message["total"])
    next_cursor = cursor + len(entries) if cursor + len(entries) < total else None
    return entries, next_cursor


async def _search_page(
    server: str, http: httpx.AsyncClient, term: str, page_size: int, cursor: int
) -> tuple[list[dict], int | None]:
    """Shared local-query implementation for both search_page_biorxiv and search_page_medrxiv --
    `http` is unused (satisfying the uniform PageFunc signature every registry entry shares); the real
    work is a local Postgres query against external_refs, scoped to only THIS server's own harvested
    rows via the external_ids->>'<server>' IS NOT NULL filter -- a bare title match against the whole
    external_refs table would also return every other source's rows, including the other bioRxiv/
    medRxiv-family source. Words ANDed, matched against either title or abstract, the same shape as
    arxiv.py's/iacr_eprint.py's own title_words() usage."""
    words = title_words(term)
    if not words:
        return [], None
    async with SessionLocal() as session:
        query = (
            select(ExternalRef)
            .where(ExternalRef.external_ids[server].astext.isnot(None))
            .where(and_(*(or_(ExternalRef.title.icontains(w, autoescape=True),
                              ExternalRef.abstract.icontains(w, autoescape=True)) for w in words)))
            .order_by(ExternalRef.id)
            .offset(cursor)
            .limit(page_size)
        )  # fmt: skip
        rows = (await session.execute(query)).scalars().all()
        entries = [
            {"doi": r.external_ids[server], "authors": "; ".join(r.authors),
             "date": f"{r.year}-01-01" if r.year else None, "title": r.title, "abstract": r.abstract}
            for r in rows
        ]  # fmt: skip
        next_cursor = cursor + page_size if len(rows) == page_size else None
        return entries, next_cursor


async def search_page_biorxiv(
    http: httpx.AsyncClient, term: str, page_size: int, cursor: int
) -> tuple[list[dict], int | None]:
    return await _search_page("biorxiv", http, term, page_size, cursor)


async def search_page_medrxiv(
    http: httpx.AsyncClient, term: str, page_size: int, cursor: int
) -> tuple[list[dict], int | None]:
    return await _search_page("medrxiv", http, term, page_size, cursor)
