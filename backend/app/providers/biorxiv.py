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
