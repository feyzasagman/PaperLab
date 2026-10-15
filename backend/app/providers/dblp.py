"""DBLP publication search. HTTP failures and invalid responses raise httpx.HTTPError.

Probed 2026-10-15: dblp.org and the official Trier mirror returned HTTP 202 HTML
Anubis challenges for both real and unknown queries. Unknown and malformed record
IDs also returned the challenge, so their underlying API behavior could not be
verified. Treat challenge pages as failures, never as empty search results.
The official search API documentation specifies JSON, h (max 1000) and zero-based
f pagination. No rate limit was established by these probes; requests are paced
conservatively at one per second, with no automatic retry of 429 responses.
"""

import httpx

from app.providers.arxiv import title_words
from app.providers.http import RateLimited, shared_pacer
from app.providers.openalex import json_body

BASE_URL = "https://dblp.org"
TIMEOUT = httpx.Timeout(10.0)


def new_client(
    *, email: str | None = None, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=TIMEOUT,
        transport=RateLimited(
            transport if transport is not None else httpx.AsyncHTTPTransport(),
            pacer=shared_pacer("dblp", None, 1.0),
        ),
    )


async def search_page(
    http: httpx.AsyncClient, title: str, page_size: int, cursor: int
) -> tuple[list[dict], int | None]:
    words = title_words(title)
    if not words:
        return [], None
    size = min(max(page_size, 1), 1000)
    response = await http.get(
        "/search/publ/api", params={"q": " ".join(words), "format": "json", "h": size, "f": cursor, "c": 0}
    )
    response.raise_for_status()
    try:
        result = json_body(response)["result"]
        if result["status"]["@code"] != "200":
            raise ValueError("DBLP reported an unsuccessful search")
        hits = result["hits"]
        if not isinstance(hits, dict):
            raise ValueError("Invalid DBLP hits")
        total, sent = int(hits["@total"]), int(hits["@sent"])
        raw = hits.get("hit", [])
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or sent != len(raw) or total < 0 or sent < 0:
            raise ValueError("Invalid DBLP hit count")
        entries = [hit["info"] for hit in raw]
        if any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("Invalid DBLP publication")
    except (KeyError, TypeError, ValueError) as exc:
        raise httpx.DecodingError("Invalid DBLP search response", request=response.request) from exc
    return entries, cursor + sent if sent and cursor + sent < total else None


async def search(http: httpx.AsyncClient, title: str, limit: int) -> list[dict]:
    entries, _ = await search_page(http, title, limit, 0)
    return entries
