"""Semantic Scholar over HTTP. Network failures, timeouts and error statuses (429 included) raise httpx.HTTPError.

Probed on 2026-09-16 without an API key: recommendations take a `DOI:<doi>` or `arXiv:<id>` key (an arXiv DOI is a
404), an unknown paper answers 404 with a JSON body, and the shared unauthenticated pool answers 429 when busy.
"""

from urllib.parse import quote

import httpx

from app.providers.http import RetryOn429
from app.providers.openalex import json_body

BASE_URL = "https://api.semanticscholar.org"
TIMEOUT = httpx.Timeout(10.0)
PAPER_FIELDS = "title,year,venue,authors,externalIds,openAccessPdf,citationCount,abstract,publicationTypes,journal"
# The default pool is recent papers only, and its picks for BERT were weak. all-cs also served a biology DOI.
RECOMMENDATION_POOL = "all-cs"
PAGE = 100  # references/citations page size (M7.5 D79)
# What the enrich_hits backfill reads per paper: triage data and the abstract.
BATCH_FIELDS = "externalIds,venue,journal,publicationTypes,abstract"
# Waits before asking again after a 429. The shared keyless pool is often busy for a moment, then answers.
RETRY_DELAYS = (2.0, 5.0)


def new_client(
    *,
    email: str | None = None,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_429: bool = True,
) -> httpx.AsyncClient:
    headers = {"x-api-key": api_key} if api_key else {}
    # RETRY_DELAYS is read here, not captured as a default argument, so a test's monkeypatch of this module's own
    # RETRY_DELAYS (done before calling new_client) still takes effect.
    # retry_429=False for a caller that waits on Retry-After itself (hit_enrichment).
    inner = transport or httpx.AsyncHTTPTransport()
    retrying = RetryOn429(inner, RETRY_DELAYS) if retry_429 else inner
    return httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=TIMEOUT, transport=retrying)


# ponytail: a fixed 2s then 5s retry on 429, Retry-After ignored (S2 doesn't send one). Still busy after that raises;
# a key saved in Settings → Paper sources lifts the shared pool's limit.


async def recommend(http: httpx.AsyncClient, key: str, limit: int) -> list[dict] | None:
    """Papers like `key`: "DOI:<doi>", "arXiv:<id>" or a Semantic Scholar paper id. None when S2 doesn't know it."""
    response = await http.get(
        f"/recommendations/v1/papers/forpaper/{quote(key, safe='/:')}",
        params={"from": RECOMMENDATION_POOL, "limit": limit, "fields": PAPER_FIELDS},
    )
    if response.status_code == 404:
        return None
    return json_body(response.raise_for_status())["recommendedPapers"]


async def match_title(http: httpx.AsyncClient, title: str) -> str | None:
    """The Semantic Scholar paper id of the best match for `title`, or None when nothing matches."""
    response = await http.get("/graph/v1/paper/search/match", params={"query": title, "fields": "paperId"})
    if response.status_code == 404:
        return None
    matches = json_body(response.raise_for_status()).get("data") or []
    return matches[0]["paperId"] if matches else None


async def get_paper(http: httpx.AsyncClient, key: str) -> dict | None:
    """One paper by "arXiv:<id>", "DOI:<doi>" or a Semantic Scholar id. None when S2 doesn't know it."""
    response = await http.get(f"/graph/v1/paper/{quote(key, safe='/:')}", params={"fields": PAPER_FIELDS})
    if response.status_code == 404:
        return None
    return json_body(response.raise_for_status())


async def get_papers(http: httpx.AsyncClient, keys: list[str]) -> list[dict | None]:
    return await batch(http, keys, PAPER_FIELDS)


async def batch(http: httpx.AsyncClient, ids: list[str], fields: str = BATCH_FIELDS) -> list[dict | None]:
    """Several papers in one request, in the order of `ids`; None for each one S2 doesn't know. At most 500 ids."""
    if not ids:
        return []
    response = await http.post("/graph/v1/paper/batch", params={"fields": fields}, json={"ids": ids})
    papers = json_body(response.raise_for_status())
    if not isinstance(papers, list):
        raise httpx.DecodingError("Semantic Scholar's batch answer is not a list", request=response.request)
    return papers


async def _edge(http: httpx.AsyncClient, key: str, edge: str, inner_field: str, cap: int) -> list[dict] | None:
    """Pages `edge` ("references" or "citations") `PAGE` at a time, collecting `inner_field` (citedPaper /
    citingPaper) until `cap` records are collected or a page has no `next`. None when S2 doesn't know `key` (404),
    like get_paper. An item with no inner record (a paper S2 lists but can't describe) is skipped, not a crash."""
    records: list[dict] = []
    offset = 0
    while len(records) < cap:
        response = await http.get(
            f"/graph/v1/paper/{quote(key, safe='/:')}/{edge}",
            params={"fields": PAPER_FIELDS, "limit": PAGE, "offset": offset},
        )
        if response.status_code == 404:
            return None
        body = json_body(response.raise_for_status())
        records += [record for item in body.get("data") or [] if (record := item.get(inner_field))]
        next_offset = body.get("next")
        if next_offset is None:
            break
        offset = next_offset
    return records[:cap]


async def references(http: httpx.AsyncClient, key: str, cap: int) -> list[dict] | None:
    """The papers `key` cites (cited-paper records, in the API's order), capped at `cap`. None when unknown."""
    return await _edge(http, key, "references", "citedPaper", cap)


async def citations(http: httpx.AsyncClient, key: str, cap: int) -> list[dict] | None:
    """The papers citing `key` (citing-paper records, in the API's own, unsorted order), capped at `cap` (D79: this
    direction is not paged to the end). None when unknown."""
    return await _edge(http, key, "citations", "citingPaper", cap)


async def search_page(http: httpx.AsyncClient, title: str, page_size: int, cursor: int) -> tuple[list[dict], int | None]:
    """One page of Semantic Scholar results."""
    params = {"query": title, "offset": cursor, "limit": page_size, "fields": PAPER_FIELDS}
    response = await http.get("/graph/v1/paper/search", params=params)
    body = json_body(response.raise_for_status())
    data = body.get("data", [])
    next_cursor = cursor + page_size if body.get("next") is not None else None
    return data, next_cursor
