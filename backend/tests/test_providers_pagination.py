import httpx
import pytest

from app.providers import (
    acm_dl,
    arxiv,
    biorxiv,
    core_ac,
    crossref,
    doaj,
    europe_pmc,
    hal,
    iacr_eprint,
    openaire,
    openalex,
    pmc,
    pubmed,
    semantic_scholar,
    ssrn,
    zenodo,
)

pytestmark = pytest.mark.anyio


# --- arxiv ------------------------------------


async def test_arxiv_search_page_advances_start(monkeypatch):
    seen_starts = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_starts.append(params["start"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await arxiv.search_page(http, "transformer", page_size=20, cursor=0)
        await arxiv.search_page(http, "transformer", page_size=20, cursor=20)
    assert seen_starts == [0, 20]


async def test_arxiv_next_cursor_when_page_is_full(monkeypatch):
    """arXiv signals more data by returning a full-sized page."""
    full_page = """<feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>
        <entry><id>https://arxiv.org/abs/2301.00001v1</id><title>Paper 1</title><published>2023-01-01T00:00:00Z</published></entry>
        <entry><id>https://arxiv.org/abs/2301.00002v1</id><title>Paper 2</title><published>2023-01-02T00:00:00Z</published></entry>
    </feed>"""

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text=full_page, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await arxiv.search_page(http, "learning", page_size=2, cursor=0)

    assert len(entries) == 2
    assert next_cursor == 2  # cursor + page_size because page is full-sized


async def test_arxiv_next_cursor_when_page_is_short(monkeypatch):
    """arXiv signals no more data by returning a short page."""
    short_page = """<feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>
        <entry><id>https://arxiv.org/abs/2301.00001v1</id><title>Paper 1</title><published>2023-01-01T00:00:00Z</published></entry>
    </feed>"""

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text=short_page, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await arxiv.search_page(http, "learning", page_size=2, cursor=10)

    assert len(entries) == 1
    assert next_cursor is None  # page is short, no more data


def _capture_arxiv(monkeypatch, entries: int) -> list[dict]:
    """Every search_page request's params; each answer holds `entries` papers."""
    seen = []
    feed = "".join(
        f"<entry><id>https://arxiv.org/abs/2301.{i:05d}v1</id><title>Paper {i}</title></entry>" for i in range(entries)
    )

    async def fake_get(self, url, params=None, **kwargs):
        seen.append(params)
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text=f"<feed xmlns='http://www.w3.org/2005/Atom'>{feed}</feed>", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return seen


async def test_arxiv_search_page_matches_any_word_in_any_field_best_first(monkeypatch):
    seen = _capture_arxiv(monkeypatch, 0)
    async with httpx.AsyncClient() as http:
        await arxiv.search_page(http, "mode collapse RLHF diversity", page_size=20, cursor=0)
        await arxiv.search_page(http, "(mode) OR collapse", page_size=20, cursor=0)
    assert seen[0]["search_query"] == "all:mode OR all:collapse OR all:rlhf OR all:diversity"
    assert (seen[0]["sortBy"], seen[0]["sortOrder"]) == ("relevance", "descending")
    assert seen[1]["search_query"] == "all:mode OR all:collapse"  # no parentheses or operators reach arXiv


async def test_arxiv_search_page_stops_at_the_result_cap(monkeypatch):
    _capture_arxiv(monkeypatch, 20)
    async with httpx.AsyncClient() as http:
        _, before_cap = await arxiv.search_page(http, "learning", page_size=20, cursor=arxiv.MAX_SEARCH_RESULTS - 40)
        _, at_cap = await arxiv.search_page(http, "learning", page_size=20, cursor=arxiv.MAX_SEARCH_RESULTS - 20)
    assert before_cap == arxiv.MAX_SEARCH_RESULTS - 20
    assert at_cap is None  # full page, but an OR query never runs dry


async def test_arxiv_search_page_of_stopwords_sends_nothing(monkeypatch):
    seen = _capture_arxiv(monkeypatch, 0)
    async with httpx.AsyncClient() as http:
        assert await arxiv.search_page(http, "the of and", page_size=20, cursor=0) == ([], None)
    assert seen == []


# --- core_ac ------------------------------------


async def test_core_ac_search_page_uses_offset(monkeypatch):
    seen_offsets = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_offsets.append(params["offset"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"totalHits": 0, "results": []}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await core_ac.search_page(http, "transformer", page_size=20, cursor=0)
        await core_ac.search_page(http, "transformer", page_size=20, cursor=20)
    assert seen_offsets == [0, 20]


async def test_core_ac_next_cursor_when_more_data_available(monkeypatch):
    """CORE signals more data via totalHits comparison."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        # totalHits > cursor + page_size means more data available
        return httpx.Response(200, json={"totalHits": 100, "results": [{"id": "1"}, {"id": "2"}]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await core_ac.search_page(http, "transformer", page_size=20, cursor=0)

    assert len(entries) == 2
    assert next_cursor == 20  # cursor + page_size < totalHits


async def test_core_ac_next_cursor_when_exhausted(monkeypatch):
    """CORE signals no more data when cursor + page_size >= totalHits."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"totalHits": 50, "results": [{"id": "1"}]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await core_ac.search_page(http, "transformer", page_size=20, cursor=40)

    assert len(entries) == 1
    assert next_cursor is None  # cursor + page_size >= totalHits


# --- pubmed ------------------------------------


async def test_pubmed_search_page_advances_retstart(monkeypatch):
    seen_starts = []

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        seen_starts.append(params["retstart"])
        return httpx.Response(200, json={"esearchresult": {"count": "100", "idlist": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await pubmed.search_page(http, "cancer", page_size=20, cursor=0)
        await pubmed.search_page(http, "cancer", page_size=20, cursor=20)
    assert seen_starts == [0, 20]


async def test_pubmed_next_cursor_when_more_remain_per_ncbis_own_count(monkeypatch):
    """Unlike arXiv (page-fullness heuristic), PubMed trusts esearch's own reported total directly."""

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        if "id" not in params:  # esearch
            return httpx.Response(200, json={"esearchresult": {"count": "45", "idlist": ["1", "2"]}}, request=request)
        return httpx.Response(200, text="<PubmedArticleSet></PubmedArticleSet>", request=request)  # efetch

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await pubmed.search_page(http, "cancer", page_size=20, cursor=20)

    assert next_cursor == 40  # 20 + 20 < 45


async def test_pubmed_next_cursor_is_none_once_the_count_is_exhausted(monkeypatch):
    """PubMed signals no more data once cursor + page_size reaches NCBI's own reported count."""

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        if "id" not in params:  # esearch
            return httpx.Response(200, json={"esearchresult": {"count": "45", "idlist": ["1"]}}, request=request)
        return httpx.Response(200, text="<PubmedArticleSet></PubmedArticleSet>", request=request)  # efetch

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await pubmed.search_page(http, "cancer", page_size=20, cursor=40)

    assert next_cursor is None  # 40 + 20 = 60, not < 45


async def test_pubmed_next_cursor_never_exceeds_ncbis_9999_retstart_ceiling(monkeypatch):
    """NCBI's ESearch hard ceiling: retstart can't exceed 9998. A count far past it must not produce a
    next_cursor past the ceiling, even though cursor + page_size < count would otherwise say there's more."""

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        if "id" not in params:  # esearch
            return httpx.Response(
                200, json={"esearchresult": {"count": "5700000", "idlist": ["1"]}}, request=request
            )
        return httpx.Response(200, text="<PubmedArticleSet></PubmedArticleSet>", request=request)  # efetch

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await pubmed.search_page(http, "cancer", page_size=20, cursor=9980)

    assert next_cursor is None  # 9980 + 20 = 10000, past NCBI's 9999 ceiling, even though count says there's more


# --- crossref ------------------------------------


async def test_crossref_search_page_uses_offset(monkeypatch):
    seen_offsets = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_offsets.append(params["offset"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"message": {"items": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await crossref.search_page(http, "transformer", page_size=20, cursor=0)
        await crossref.search_page(http, "transformer", page_size=20, cursor=20)
    assert seen_offsets == [0, 20]


async def test_crossref_next_cursor_when_page_is_full(monkeypatch):
    """Crossref signals more data by returning a full-sized page."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"message": {"items": [{"DOI": "1"}, {"DOI": "2"}]}},
            request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await crossref.search_page(http, "transformer", page_size=2, cursor=0)

    assert len(entries) == 2
    assert next_cursor == 2  # cursor + page_size because page is full-sized


async def test_crossref_next_cursor_when_page_is_short(monkeypatch):
    """Crossref signals no more data by returning a short page."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"message": {"items": [{"DOI": "1"}]}},
            request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await crossref.search_page(http, "transformer", page_size=2, cursor=10)

    assert len(entries) == 1
    assert next_cursor is None  # page is short, no more data


# --- semantic_scholar ------------------------------------


async def test_semantic_scholar_search_page_uses_offset(monkeypatch):
    seen_offsets = []
    seen_fields = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_offsets.append(params["offset"])
        seen_fields.append(params["fields"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"total": 0, "data": [], "next": None}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await semantic_scholar.search_page(http, "transformer", page_size=20, cursor=0)
        await semantic_scholar.search_page(http, "transformer", page_size=20, cursor=20)
    assert seen_offsets == [0, 20]
    # A bare `",".join(PAPER_FIELDS)` on the (string, not list) constant would insert a comma between every
    # character instead of the field names — this pins the real, whole-field-list value the request must carry.
    assert seen_fields == [semantic_scholar.PAPER_FIELDS, semantic_scholar.PAPER_FIELDS]


async def test_semantic_scholar_next_cursor_when_next_field_present(monkeypatch):
    """Semantic Scholar signals more data via presence of 'next' field."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"total": 100, "data": [{"paperId": "1"}, {"paperId": "2"}], "next": 20},
            request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await semantic_scholar.search_page(http, "transformer", page_size=20, cursor=0)

    assert len(entries) == 2
    assert next_cursor == 20  # body.get("next") is not None


async def test_semantic_scholar_next_cursor_when_next_field_absent(monkeypatch):
    """Semantic Scholar signals no more data by omitting 'next' field."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"total": 50, "data": [{"paperId": "1"}]},  # no "next" field
            request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await semantic_scholar.search_page(http, "transformer", page_size=20, cursor=20)

    assert len(entries) == 1
    assert next_cursor is None  # "next" field is missing


# --- openalex ------------------------------------


async def test_openalex_search_page_uses_page_number(monkeypatch):
    seen_pages = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_pages.append(params["page"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"meta": {"count": 0}, "results": []}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await openalex.search_page(http, "transformer", page_size=20, cursor=1)
        await openalex.search_page(http, "transformer", page_size=20, cursor=2)
    assert seen_pages == [1, 2]


async def test_openalex_next_cursor_when_more_data_available(monkeypatch):
    """OpenAlex signals more data via meta.count comparison with cursor * page_size."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        # cursor * page_size < meta.count means more data
        return httpx.Response(
            200,
            json={"meta": {"count": 100}, "results": [{"id": "W1"}, {"id": "W2"}]},
            request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await openalex.search_page(http, "transformer", page_size=20, cursor=1)

    assert len(entries) == 2
    assert next_cursor == 2  # cursor + 1 because 1 * 20 < 100


async def test_openalex_next_cursor_when_exhausted(monkeypatch):
    """OpenAlex signals no more data when cursor * page_size >= meta.count."""
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"meta": {"count": 50}, "results": [{"id": "W1"}]},
            request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        entries, next_cursor = await openalex.search_page(http, "transformer", page_size=20, cursor=3)

    assert len(entries) == 1
    assert next_cursor is None  # 3 * 20 >= 50


# --- pmc ------------------------------------


async def test_pmc_search_page_advances_retstart(monkeypatch):
    seen_starts = []

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        seen_starts.append(params["retstart"])
        return httpx.Response(200, json={"esearchresult": {"count": "100", "idlist": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await pmc.search_page(http, "cancer", page_size=20, cursor=0)
        await pmc.search_page(http, "cancer", page_size=20, cursor=20)
    assert seen_starts == [0, 20]


async def test_pmc_next_cursor_when_more_remain_per_ncbis_own_count(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        if "id" not in params:  # esearch
            return httpx.Response(200, json={"esearchresult": {"count": "45", "idlist": ["1", "2"]}}, request=request)
        return httpx.Response(200, text="<pmc-articleset></pmc-articleset>", request=request)  # efetch

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await pmc.search_page(http, "cancer", page_size=20, cursor=20)

    assert next_cursor == 40  # 20 + 20 < 45


async def test_pmc_next_cursor_is_none_once_the_count_is_exhausted(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        if "id" not in params:
            return httpx.Response(200, json={"esearchresult": {"count": "45", "idlist": ["1"]}}, request=request)
        return httpx.Response(200, text="<pmc-articleset></pmc-articleset>", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await pmc.search_page(http, "cancer", page_size=20, cursor=40)

    assert next_cursor is None  # 40 + 20 = 60, not < 45


async def test_pmc_next_cursor_never_exceeds_ncbis_9999_retstart_ceiling(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        if "id" not in params:
            return httpx.Response(
                200, json={"esearchresult": {"count": "5700000", "idlist": ["1"]}}, request=request
            )
        return httpx.Response(200, text="<pmc-articleset></pmc-articleset>", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await pmc.search_page(http, "cancer", page_size=20, cursor=9980)

    assert next_cursor is None  # 9980 + 20 = 10000, past NCBI's 9999 ceiling


# --- europe_pmc ------------------------------------


async def test_europe_pmc_search_page_sends_the_cursor_mark_it_was_given(monkeypatch):
    seen_cursors = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_cursors.append(params["cursorMark"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await europe_pmc.search_page(http, "cancer", page_size=20, cursor="*")
        await europe_pmc.search_page(http, "cancer", page_size=20, cursor="AoIIQDaIlCg1NjQ3MDk5NA==")
    assert seen_cursors == ["*", "AoIIQDaIlCg1NjQ3MDk5NA=="]


async def test_europe_pmc_next_cursor_is_the_servers_own_nextcursormark(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={
                "hitCount": 45, "nextCursorMark": "AoIIQDaIlCg1NjQ3MDk5NA==",
                "resultList": {"result": [{"id": "1", "title": "x"}]},
            },
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await europe_pmc.search_page(http, "cancer", page_size=20, cursor="*")

    assert next_cursor == "AoIIQDaIlCg1NjQ3MDk5NA=="


async def test_europe_pmc_next_cursor_is_none_once_nextcursormark_disappears(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200, json={"hitCount": 45, "resultList": {"result": [{"id": "1", "title": "x"}]}}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await europe_pmc.search_page(http, "cancer", page_size=20, cursor="*")

    assert next_cursor is None


# --- zenodo ------------------------------------


async def test_zenodo_search_page_sends_the_page_it_was_given(monkeypatch):
    seen_pages = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_pages.append(params["page"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"hits": {"hits": []}, "links": {}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await zenodo.search_page(http, "cancer", page_size=20, cursor=1)
        await zenodo.search_page(http, "cancer", page_size=20, cursor=2)
    assert seen_pages == [1, 2]


async def test_zenodo_next_cursor_is_the_next_page_when_links_next_is_present(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200, json={"hits": {"hits": [{"id": 1}]}, "links": {"next": "https://zenodo.org/api/records?page=2"}},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await zenodo.search_page(http, "cancer", page_size=20, cursor=1)

    assert next_cursor == 2


async def test_zenodo_next_cursor_is_none_once_links_next_disappears(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"hits": {"hits": [{"id": 1}]}, "links": {}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await zenodo.search_page(http, "cancer", page_size=20, cursor=5)

    assert next_cursor is None


# --- hal ------------------------------------


async def test_hal_search_page_advances_start(monkeypatch):
    seen_starts = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_starts.append(params["start"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"response": {"numFound": 100, "docs": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await hal.search_page(http, "cancer", page_size=20, cursor=0)
        await hal.search_page(http, "cancer", page_size=20, cursor=20)
    assert seen_starts == [0, 20]


async def test_hal_next_cursor_when_more_remain_per_hals_own_numfound(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200, json={"response": {"numFound": 45, "docs": [{"docid": "1"}, {"docid": "2"}]}}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await hal.search_page(http, "cancer", page_size=20, cursor=20)

    assert next_cursor == 40  # 20 + 20 < 45


async def test_hal_next_cursor_is_none_once_numfound_is_exhausted(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"response": {"numFound": 45, "docs": [{"docid": "1"}]}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await hal.search_page(http, "cancer", page_size=20, cursor=40)

    assert next_cursor is None  # 40 + 20 = 60, not < 45


async def test_hal_search_page_has_no_artificial_ceiling_past_10000(monkeypatch):
    """The architecture doc's claimed 10,000-result cap does not exist (confirmed live) -- a huge
    numFound keeps paging normally well past where PubMed/PMC's own real 9999 NCBI ceiling would stop."""

    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200, json={"response": {"numFound": 89136, "docs": [{"docid": "1"}]}}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await hal.search_page(http, "cancer", page_size=20, cursor=80000)

    assert next_cursor == 80020  # no cap -- 80000 + 20 < 89136


# --- acm_dl ------------------------------------


async def test_acm_dl_search_page_sends_the_offset_it_was_given(monkeypatch):
    seen_offsets = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_offsets.append(params["offset"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"message": {"items": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await acm_dl.search_page(http, "credential leakage", page_size=5, cursor=0)
        await acm_dl.search_page(http, "credential leakage", page_size=5, cursor=5)
    assert seen_offsets == [0, 5]


async def test_acm_dl_search_page_keeps_the_acm_filter_on_every_request(monkeypatch):
    seen_filters = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_filters.append(params["filter"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"message": {"items": []}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await acm_dl.search_page(http, "x", page_size=5, cursor=0)
        await acm_dl.search_page(http, "x", page_size=5, cursor=5)
    assert seen_filters == ["prefix:10.1145", "prefix:10.1145"]


async def test_acm_dl_next_cursor_is_none_once_a_short_page_comes_back(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        item = {"DOI": "10.1145/1", "type": "journal-article", "title": ["One"]}
        return httpx.Response(200, json={"message": {"items": [item]}}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await acm_dl.search_page(http, "x", page_size=5, cursor=0)

    assert next_cursor is None  # 1 item came back for a page_size=5 request: Crossref's own "no more" signal


# --- ssrn ------------------------------------


async def test_ssrn_search_page_converts_the_offset_to_a_page_number(monkeypatch):
    seen_pages = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_pages.append(params["page"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"meta": {"count": 100}, "results": []}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await ssrn.search_page(http, "x", page_size=25, cursor=0)
        await ssrn.search_page(http, "x", page_size=25, cursor=50)
    assert seen_pages == [1, 3]  # offset 0 -> page 1; offset 50 / page_size 25 + 1 -> page 3


async def test_ssrn_search_page_keeps_the_ssrn_filter_on_every_request(monkeypatch):
    seen_filters = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_filters.append(params["filter"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"meta": {"count": 0}, "results": []}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await ssrn.search_page(http, "diffusion innovation", page_size=25, cursor=0)
        await ssrn.search_page(http, "diffusion innovation", page_size=25, cursor=25)

    assert seen_filters == [
        "title.search:diffusion innovation,locations.source.id:S4210172589",
        "title.search:diffusion innovation,locations.source.id:S4210172589",
    ]


async def test_ssrn_next_cursor_when_more_remain_per_openalexs_own_count(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200, json={"meta": {"count": 10}, "results": [{"id": "https://openalex.org/W1"}]}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await ssrn.search_page(http, "x", page_size=1, cursor=0)

    assert next_cursor == 1


async def test_ssrn_next_cursor_is_none_once_count_is_exhausted(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200, json={"meta": {"count": 1}, "results": [{"id": "https://openalex.org/W1"}]}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await ssrn.search_page(http, "x", page_size=1, cursor=0)

    assert next_cursor is None


# --- doaj ------------------------------------


async def test_doaj_search_page_sends_the_page_it_was_given(monkeypatch):
    seen_pages = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_pages.append(params["page"])
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"total": 0, "page": params["page"], "results": []}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await doaj.search_page(http, "cancer", page_size=20, cursor=1)
        await doaj.search_page(http, "cancer", page_size=20, cursor=2)
    assert seen_pages == [1, 2]


async def test_doaj_next_cursor_is_the_next_page_when_next_is_present(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        body = {"total": 2, "page": 1, "results": [{"id": "1" * 32}], "next": "https://doaj.org/api/v4/x?page=2"}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await doaj.search_page(http, "cancer", page_size=1, cursor=1)

    assert next_cursor == 2


async def test_doaj_next_cursor_is_none_once_next_disappears(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"total": 2, "page": 2, "results": [{"id": "2" * 32}]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await doaj.search_page(http, "cancer", page_size=1, cursor=2)

    assert next_cursor is None


# --- openaire ------------------------------------


async def test_openaire_search_page_sends_the_page_it_was_given(monkeypatch):
    seen_pages = []

    async def fake_get(self, url, params=None, **kwargs):
        seen_pages.append(params["page"])
        request = httpx.Request("GET", url, params=params)
        body = {"header": {"numFound": 0, "page": params["page"], "pageSize": params["pageSize"]}, "results": []}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        await openaire.search_page(http, "CRISPR", page_size=20, cursor=1)
        await openaire.search_page(http, "CRISPR", page_size=20, cursor=2)
    assert seen_pages == [1, 2]


async def test_openaire_next_cursor_when_more_remain_per_its_own_numfound(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        body = {"header": {"numFound": 10, "page": 1, "pageSize": 1}, "results": [{"id": "x" * 32}]}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        _, next_cursor = await openaire.search_page(http, "CRISPR", page_size=1, cursor=1)

    assert next_cursor == 2


async def test_openaire_next_cursor_is_none_once_numfound_is_exhausted(monkeypatch):
    async def fake_get(self, url, params=None, **kwargs):
        request = httpx.Request("GET", url, params=params)
        body = {"header": {"numFound": 3, "page": 2, "pageSize": 2}, "results": [{"id": "x" * 32}]}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as http:
        # 2 * 2 = 4 is not < 3, so None. Without the page_size factor, 2 < 3 would wrongly return a cursor.
        _, next_cursor = await openaire.search_page(http, "CRISPR", page_size=2, cursor=2)

    assert next_cursor is None


# --- iacr_eprint ------------------------------------


async def test_iacr_eprint_search_page_only_returns_rows_this_source_harvested(session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.models import ExternalRef

    @asynccontextmanager
    async def _reuse_test_session():
        yield session  # never closes it -- the `session` fixture's own teardown does that

    monkeypatch.setattr(iacr_eprint, "SessionLocal", _reuse_test_session)

    session.add(ExternalRef(title="An IACR Paper", external_ids={"iacr_eprint": "2026/7001"}, sources=["iacr_eprint"]))
    session.add(ExternalRef(title="A Different Source's Paper", external_ids={"doaj": "0" * 32}, sources=["doaj"]))
    await session.flush()

    entries, _ = await iacr_eprint.search_page(None, "Paper", 10, 0)

    assert len(entries) == 1
    assert entries[0]["identifier"] == "oai:eprint.iacr.org:2026/7001"


async def test_iacr_eprint_search_page_paginates_with_a_plain_offset(session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.models import ExternalRef

    @asynccontextmanager
    async def _reuse_test_session():
        yield session

    monkeypatch.setattr(iacr_eprint, "SessionLocal", _reuse_test_session)

    for i in range(3):
        session.add(
            ExternalRef(title=f"Paper {i}", external_ids={"iacr_eprint": f"2026/800{i}"}, sources=["iacr_eprint"])
        )
    await session.flush()

    page_1, cursor_1 = await iacr_eprint.search_page(None, "Paper", 2, 0)
    page_2, cursor_2 = await iacr_eprint.search_page(None, "Paper", 2, cursor_1)

    assert len(page_1) == 2
    assert cursor_1 == 2
    assert len(page_2) == 1
    assert cursor_2 is None


# --- biorxiv / medrxiv ------------------------------------


async def test_biorxiv_search_page_only_returns_biorxivs_own_rows_not_medrxivs_or_anyone_elses(session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.models import ExternalRef

    @asynccontextmanager
    async def _reuse_test_session():
        yield session

    monkeypatch.setattr(biorxiv, "SessionLocal", _reuse_test_session)

    session.add(
        ExternalRef(title="A bioRxiv Paper", external_ids={"biorxiv": "10.1101/2026.10.01.999010"}, sources=["biorxiv"])
    )
    session.add(
        ExternalRef(title="A medRxiv Paper", external_ids={"medrxiv": "10.1101/2026.10.01.999011"}, sources=["medrxiv"])
    )
    session.add(ExternalRef(title="A Different Source's Paper", external_ids={"doaj": "0" * 32}, sources=["doaj"]))
    await session.flush()

    entries, _ = await biorxiv.search_page_biorxiv(None, "Paper", 10, 0)

    assert len(entries) == 1
    assert entries[0]["doi"] == "10.1101/2026.10.01.999010"


async def test_medrxiv_search_page_paginates_with_a_plain_offset(session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.models import ExternalRef

    @asynccontextmanager
    async def _reuse_test_session():
        yield session

    monkeypatch.setattr(biorxiv, "SessionLocal", _reuse_test_session)

    for i in range(3):
        session.add(
            ExternalRef(
                title=f"Study {i}", external_ids={"medrxiv": f"10.1101/2026.10.01.99902{i}"}, sources=["medrxiv"]
            )
        )
    await session.flush()

    page_1, cursor_1 = await biorxiv.search_page_medrxiv(None, "Study", 2, 0)
    page_2, cursor_2 = await biorxiv.search_page_medrxiv(None, "Study", 2, cursor_1)

    assert len(page_1) == 2
    assert cursor_1 == 2
    assert len(page_2) == 1
    assert cursor_2 is None


async def test_biorxiv_search_page_preserves_year_through_from_biorxiv_entry(session, monkeypatch):
    """The local query's own synthesized `date` field (reconstructed from the stored year, since bioRxiv/
    medRxiv's external_ids value is a bare DOI with no parseable year) must round-trip correctly back
    through from_biorxiv_entry -- unlike IACR ePrint, this source has no id-prefix fallback for year."""
    from contextlib import asynccontextmanager

    from app.core.candidates import from_biorxiv_entry
    from app.models import ExternalRef

    @asynccontextmanager
    async def _reuse_test_session():
        yield session

    monkeypatch.setattr(biorxiv, "SessionLocal", _reuse_test_session)

    session.add(ExternalRef(
        title="Zzyxqplorp Fixturewidget Preprint", year=2025, authors=["Ada Fixture"],
        external_ids={"biorxiv": "10.1101/2026.10.01.999030"}, sources=["biorxiv"],
    ))
    await session.flush()

    entries, _ = await biorxiv.search_page_biorxiv(None, "Zzyxqplorp Fixturewidget", 10, 0)
    [entry] = entries

    assert from_biorxiv_entry(entry, server="biorxiv").year == 2025
