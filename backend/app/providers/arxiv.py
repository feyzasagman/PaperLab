"""arXiv over HTTP. Network failures, timeouts and error statuses (429 included) raise httpx.HTTPError.

Probed on 2026-09-17: answers are Atom feeds. An unknown but well-formed ID is an empty feed; a malformed one (the
discovery regex lets `hep-th/9999999` through) is a 400 whose feed holds one entry titled "Error". arXiv asks for at
most one request every 3 seconds; a long run of requests 3.1 seconds apart got 429.
"""

import re
import xml.etree.ElementTree as ET

import httpx

BASE_URL = "https://export.arxiv.org"
TIMEOUT = httpx.Timeout(10.0)
MAX_TITLE_WORDS = 12
# ponytail: a search run's OR query never runs dry, so paging stops here; past this arXiv's relevance order is noise.
# Raise it if runs show relevant papers still arriving on the last page.
MAX_SEARCH_RESULTS = 200
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
# arXiv's title index leaves these out, so `ti:of` matches nothing and empties the whole AND query (probed
# 2026-09-17, each against `ti:learning`). CORE's parser reads and/or/not, in any case, as operators and answers 500.
STOPWORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such that the their then there these they "
    "this to was will with".split()
)
# An "Error" entry's <id> is https://arxiv.org/api/errors#…, so only real papers match.
_ABS_ID = re.compile(r"arxiv\.org/abs/(.+?)(?:v\d+)?$")


def new_client(
    *, email: str | None = None, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT, transport=transport)


# ponytail: no retry on 429, as in openalex.py, and nothing spaces requests 3 seconds apart: a reader searching faster
# than that gets arXiv's busy notice. Add a per-host throttle if that notice shows up in normal use.


def title_words(title: str) -> list[str]:
    """The words a title search ANDs together: lowercase, no stopwords, at most MAX_TITLE_WORDS. CORE uses it too."""
    return [word for word in re.findall(r"\w+", title.lower()) if word not in STOPWORDS][:MAX_TITLE_WORDS]


async def search(http: httpx.AsyncClient, title: str, limit: int) -> list[dict]:
    # `ti:` word by word, not a quoted phrase, so a partial title still matches. `all:` ranked unrelated papers first.
    words = title_words(title)
    if not words:
        return []
    query = " AND ".join(f"ti:{word}" for word in words)
    response = await http.get("/api/query", params={"search_query": query, "max_results": limit})
    return _entries(response.raise_for_status())


async def get(http: httpx.AsyncClient, arxiv_id: str) -> dict | None:
    """One paper by arXiv ID, or None when arXiv has no such ID or rejects its format."""
    response = await http.get("/api/query", params={"id_list": arxiv_id})
    if response.status_code == 400:  # the ID is the only thing sent, so a 400 means arXiv can't read it
        return None
    entries = _entries(response.raise_for_status())
    return entries[0] if entries else None


async def search_page(
    http: httpx.AsyncClient, topic: str, page_size: int, cursor: int
) -> tuple[list[dict], int | None]:
    """One page of a search run's arXiv results: any word of `topic` in any field, best first. `cursor` is `start`;
    returns (entries, next_start_or_None).

    Not search's every-word-in-the-title form: for "mode collapse RLHF diversity" that gave 0, and every word in any
    field gave 5 and missed the two papers a reader wants, which this form ranks in its top 8 (probed 2026-10-15)."""
    words = title_words(topic)
    if not words:
        return [], None
    query = " OR ".join(f"all:{word}" for word in words)
    params = {
        "search_query": query, "start": cursor, "max_results": page_size,
        "sortBy": "relevance", "sortOrder": "descending",
    }  # fmt: skip
    response = await http.get("/api/query", params=params)
    entries = _entries(response.raise_for_status())
    more = len(entries) == page_size and cursor + page_size < MAX_SEARCH_RESULTS
    next_cursor = cursor + page_size if more else None
    return entries, next_cursor


def _entries(response: httpx.Response) -> list[dict]:
    try:
        feed = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise httpx.DecodingError(str(exc), request=response.request) from exc
    if feed.tag != f"{{{NS['atom']}}}feed":
        raise httpx.DecodingError("arXiv's answer is not an Atom feed", request=response.request)
    return [entry for node in feed.iterfind("atom:entry", NS) if (entry := _entry(node))]


def _entry(node: ET.Element) -> dict | None:
    match = _ABS_ID.search(node.findtext("atom:id", "", NS).strip())
    if not match:
        return None
    published = node.findtext("atom:published", "", NS)
    doi = node.findtext("arxiv:doi", "", NS).strip()
    return {
        "arxiv_id": match.group(1).lower(),
        "title": " ".join(node.findtext("atom:title", "", NS).split()),
        "authors": [
            joined
            for name in node.iterfind("atom:author/atom:name", NS)
            if (joined := " ".join((name.text or "").split()))
        ],
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "doi": doi.lower() or None,
        "journal_ref": " ".join(node.findtext("arxiv:journal_ref", "", NS).split()) or None,
        "abstract": " ".join(node.findtext("atom:summary", "", NS).split()) or None,
    }
