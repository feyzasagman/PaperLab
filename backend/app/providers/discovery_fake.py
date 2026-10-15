"""Find papers and Similar without the network, for the E2E stack (DISCOVERY_PROVIDER=fake).

Three papers: one with a free PDF, one whose "PDF" link is a web page, and one with no free copy. They have DOIs under
the 10.5555 test prefix and no OpenAlex IDs, so adding one never collides with a real paper's OpenAlex ID.
OpenAlex, Crossref and Semantic Scholar answer all three. arXiv and CORE answer only the free paper: an arXiv ID would
give the closed paper a PDF link. Unpaywall lists the free and landing papers' links, so with OpenAlex off (the default,
and the E2E stack's setting) the badges match M19's. arxiv.org serves no PDF, so Add falls through to the fixture host.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

import httpx
import pymupdf

MAILTO = "e2e@paperlab.test"
PDF_HOST = "https://pdf.paperlab.test"
FREE_TITLE = "PaperLab Find Papers Fixture"
FREE_ARXIV_ID = "2609.00001"
LANDING_TITLE = "PaperLab Landing Page Fixture"
CLOSED_TITLE = "PaperLab Closed Access Fixture"
DOI_PREFIX = "10.5555/paperlab-e2e-"
PAPERS = [
    (FREE_TITLE, f"{DOI_PREFIX}free", f"{PDF_HOST}/paper.pdf"),
    (LANDING_TITLE, f"{DOI_PREFIX}landing", f"{PDF_HOST}/page.html"),
    (CLOSED_TITLE, f"{DOI_PREFIX}closed", None),
]
_BODY = "Finding papers starts from a title or an identifier. " * 12


@cache
def pdf_bytes() -> bytes:
    """One page: a bold 18pt title extraction takes as the paper's title, and enough prose to chunk."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), FREE_TITLE, fontsize=18, fontname="hebo")
    page.insert_textbox(pymupdf.Rect(72, 100, 520, 400), _BODY, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _work(title: str, doi: str, pdf_url: str | None) -> dict:
    location = {"pdf_url": pdf_url, "landing_page_url": f"https://doi.org/{doi}"}
    return {
        "doi": f"https://doi.org/{doi}",
        "title": title,
        "publication_year": 2026,
        "cited_by_count": 3,
        "authorships": [{"author": {"display_name": "Ada Fixture"}}],
        "primary_location": {"source": {"display_name": "Journal of Fixtures"}},
        "best_oa_location": location if pdf_url else None,
        "locations": [location],
    }


def _s2_paper(index: int, title: str, doi: str, pdf_url: str | None) -> dict:
    return {
        "paperId": f"{index + 1:040x}",
        "title": title,
        "year": 2026,
        "venue": "Journal of Fixtures",
        "authors": [{"name": "Ada Fixture"}],
        "externalIds": {"DOI": doi},
        "openAccessPdf": {"url": pdf_url or ""},
        "citationCount": 3,
    }


def _crossref_item(title: str, doi: str, pdf_url: str | None) -> dict:
    return {
        "DOI": doi,
        "type": "journal-article",
        "title": [title],
        "author": [{"given": "Ada", "family": "Fixture"}],
        "issued": {"date-parts": [[2026]]},
        "container-title": ["Journal of Fixtures"],
        "is-referenced-by-count": 3,
    }


def _arxiv_feed() -> str:
    title, doi, _ = PAPERS[0]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/{FREE_ARXIV_ID}v1</id>
    <published>2026-01-05T00:00:00Z</published>
    <title>{title}</title>
    <author><name>Ada Fixture</name></author>
    <arxiv:doi>{doi}</arxiv:doi>
  </entry>
</feed>"""


def _core_work() -> dict:
    title, doi, pdf_url = PAPERS[0]
    return {"id": 900001, "title": title, "authors": [{"name": "Fixture, Ada"}], "yearPublished": 2026, "doi": doi,
            "arxivId": None, "downloadUrl": pdf_url}  # fmt: skip


# search_page() pagination (Task 11): a separate, small fixture list distinct from PAPERS, so a paginated search
# request never disturbs the single-page Find Papers / Similar / references fixtures above. Every provider's
# search_page() is exercised with page_size=2 against these 3 items, which exhausts on page 2 (a short page) for
# every next_cursor rule below, OpenAlex's 1-based `page` cursor included.
PAGE_PAPERS = [(f"PaperLab Pagination Fixture {n}", f"{DOI_PREFIX}page-{n}") for n in (1, 2, 3)]

# arXiv ids and S2 paperIds below are generated positionally, same as PAPERS' own (FREE_ARXIV_ID, _s2_paper(i, ...)
# via enumerate(PAPERS)). Offset by these bases so PAGE_PAPERS never lands on PAPERS' 2609.0000{1,2,3} / hex(1,2,3).
_PAGE_ARXIV_INDEX_BASE = 10_000
_PAGE_S2_INDEX_BASE = 1_000


def _page_slice(offset: int, limit: int) -> list[tuple[str, str]]:
    return PAGE_PAPERS[offset : offset + limit]


def _openalex_search_page(params: httpx.QueryParams) -> httpx.Response:
    page, page_size = int(params["page"]), int(params["per-page"])
    items = _page_slice((page - 1) * page_size, page_size)
    return httpx.Response(200, json={
        "meta": {"count": len(PAGE_PAPERS)}, "results": [_work(title, doi, None) for title, doi in items],
    })  # fmt: skip


def _crossref_search_page(params: httpx.QueryParams) -> httpx.Response:
    items = _page_slice(int(params["offset"]), int(params["rows"]))
    return httpx.Response(200, json={"message": {"items": [_crossref_item(title, doi, None) for title, doi in items]}})


def _arxiv_search_page_feed(params: httpx.QueryParams) -> str:
    offset, page_size = int(params["start"]), int(params["max_results"])
    entries = "".join(
        f"""
  <entry>
    <id>http://arxiv.org/abs/2609.{index:05d}v1</id>
    <published>2026-01-05T00:00:00Z</published>
    <title>{title}</title>
    <summary>{title} reports a controlled study of pagination in fixture search results.</summary>
    <author><name>Ada Fixture</name></author>
    <arxiv:doi>{doi}</arxiv:doi>
  </entry>"""
        for index, (title, doi) in enumerate(_page_slice(offset, page_size), start=_PAGE_ARXIV_INDEX_BASE + offset + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">{entries}
</feed>"""


def _page_core_work(core_id: int, title: str, doi: str) -> dict:
    return {"id": core_id, "title": title, "authors": [{"name": "Fixture, Ada"}], "yearPublished": 2026, "doi": doi,
            "arxivId": None, "downloadUrl": None}  # fmt: skip


def _core_search_page(params: httpx.QueryParams) -> httpx.Response:
    offset, page_size = int(params["offset"]), int(params["limit"])
    results = [
        _page_core_work(900100 + offset + i, title, doi)
        for i, (title, doi) in enumerate(_page_slice(offset, page_size))
    ]
    return httpx.Response(200, json={"totalHits": len(PAGE_PAPERS), "results": results})


def _s2_search_page(params: httpx.QueryParams) -> httpx.Response:
    offset, page_size = int(params["offset"]), int(params["limit"])
    items = _page_slice(offset, page_size)
    data = [_s2_paper(_PAGE_S2_INDEX_BASE + offset + i, title, doi, None) for i, (title, doi) in enumerate(items)]
    body = {"data": data}
    if offset + page_size < len(PAGE_PAPERS):
        body["next"] = offset + page_size
    return httpx.Response(200, json=body)


def _pubmed_article(pmid: str, title: str, doi: str) -> str:
    return (
        f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
        f"<Journal><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue></Journal>"
        f"<ArticleTitle>{title}</ArticleTitle>"
        f'<ELocationID EIdType="doi">{doi}</ELocationID>'
        f"<AuthorList><Author><LastName>Fixture</LastName><ForeName>Ada</ForeName></Author></AuthorList>"
        f"</Article></MedlineCitation></PubmedArticle>"
    )


def _pubmed_esearch_response(ids: list[str], count: int) -> httpx.Response:
    return httpx.Response(200, json={"esearchresult": {"count": str(count), "idlist": ids}})


def _pubmed_efetch_response(entries: list[tuple[str, str, str]]) -> httpx.Response:
    articles = "".join(_pubmed_article(pmid, title, doi) for pmid, title, doi in entries)
    return httpx.Response(
        200, text=f"<PubmedArticleSet>{articles}</PubmedArticleSet>", headers={"content-type": "application/xml"}
    )


# PMIDs are fabricated positionally, in two disjoint ranges so a request's own `id` list says which fixture
# set (PAPERS, for the one-shot ask flow, or PAGE_PAPERS, for a search_page pagination test) it belongs to —
# offset well clear of the other fixtures' own ids (core_id 900000s, s2 paperId hex 1-3, arxiv 10000s above).
_PMID_BASE = 800_000       # PAPERS[0..2] -> 800000..800002
_PAGE_PMID_BASE = 810_000  # PAGE_PAPERS[0..2] -> 810000..810002
FREE_PUBMED_ID = str(_PMID_BASE)  # PAPERS[0]'s own pmid — mirrors FREE_ARXIV_ID above


def _pubmed_handle(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/esearch.fcgi"):
        retmax = int(request.url.params["retmax"])
        # Unlike arxiv.py's plain search() (which sends no `start` at all, letting _arxiv_handle tell the two
        # cases apart by presence alone), pubmed.search() and search_page() both always send retstart/retmax
        # — so this tells them apart by size instead: the one-shot ask flow always asks PER_SOURCE=10 (> the
        # 3-item PAPERS fixture), while every pagination test in this file deliberately uses page_size=2 (this
        # file's own documented convention). Never call pubmed.search()/search_page() with a limit/page_size
        # that straddles len(PAPERS)==3 in a new test, or this stops being able to tell them apart.
        if retmax < len(PAPERS):  # a direct search_page() pagination test in this file (page_size=2)
            offset = int(request.url.params["retstart"])
            items = _page_slice(offset, retmax)
            ids = [str(_PAGE_PMID_BASE + offset + i) for i in range(len(items))]
            return _pubmed_esearch_response(ids, len(PAGE_PAPERS))
        ids = [str(_PMID_BASE + i) for i in range(len(PAPERS))]
        return _pubmed_esearch_response(ids, len(PAPERS))
    ids = request.url.params["id"].split(",")
    if int(ids[0]) >= _PAGE_PMID_BASE:
        offset = int(ids[0]) - _PAGE_PMID_BASE
        items = _page_slice(offset, len(ids))
        return _pubmed_efetch_response([(pmid, title, doi) for pmid, (title, doi) in zip(ids, items)])
    return _pubmed_efetch_response([(str(_PMID_BASE + i), title, doi) for i, (title, doi, _) in enumerate(PAPERS)])


# PMC shares PubMed's exact esearch/efetch request shape (just db=pmc) and so mirrors _pubmed_handle almost
# line for line below -- own id bases, offset past PubMed's 800000/810000, so fixture ids never collide.
_PMC_PMID_BASE = 820_000       # PAPERS[0..2] -> 820000..820002
_PAGE_PMC_PMID_BASE = 830_000  # PAGE_PAPERS[0..2] -> 830000..830002
FREE_PMC_ID = str(_PMC_PMID_BASE)


def _pmc_article(pmcid: str, title: str, doi: str) -> str:
    return (
        f'<article><front><article-meta>'
        f'<article-id pub-id-type="pmcid">PMC{pmcid}</article-id>'
        f'<article-id pub-id-type="doi">{doi}</article-id>'
        f"<title-group><article-title>{title}</article-title></title-group>"
        f"<pub-date><year>2026</year></pub-date>"
        f'<contrib-group><contrib contrib-type="author">'
        f"<name><surname>Fixture</surname><given-names>Ada</given-names></name></contrib></contrib-group>"
        f"</article-meta></front></article>"
    )


def _pmc_efetch_response(entries: list[tuple[str, str, str]]) -> httpx.Response:
    articles = "".join(_pmc_article(pmcid, title, doi) for pmcid, title, doi in entries)
    return httpx.Response(
        200, text=f"<pmc-articleset>{articles}</pmc-articleset>", headers={"content-type": "application/xml"}
    )


def _pmc_handle(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/esearch.fcgi"):
        retmax = int(request.url.params["retmax"])
        if retmax < len(PAPERS):  # a direct search_page() pagination test (page_size=2), same convention as PubMed
            offset = int(request.url.params["retstart"])
            items = _page_slice(offset, retmax)
            ids = [str(_PAGE_PMC_PMID_BASE + offset + i) for i in range(len(items))]
            return _pubmed_esearch_response(ids, len(PAGE_PAPERS))
        ids = [str(_PMC_PMID_BASE + i) for i in range(len(PAPERS))]
        return _pubmed_esearch_response(ids, len(PAPERS))
    ids = request.url.params["id"].split(",")
    if int(ids[0]) >= _PAGE_PMC_PMID_BASE:
        offset = int(ids[0]) - _PAGE_PMC_PMID_BASE
        items = _page_slice(offset, len(ids))
        return _pmc_efetch_response([(pmcid, title, doi) for pmcid, (title, doi) in zip(ids, items)])
    return _pmc_efetch_response([(str(_PMC_PMID_BASE + i), title, doi) for i, (title, doi, _) in enumerate(PAPERS)])


# Europe PMC is structurally simpler: one request per page, no esearch/efetch split, and its cursorMark is an
# opaque string token (not an offset) -- this fake mints its own cursorMark values (just the next offset as a
# string) since it's the only side that ever has to read them back.
_EUROPE_PMC_ID_BASE = 840_000       # PAPERS[0..2] -> 840000..840002
_PAGE_EUROPE_PMC_ID_BASE = 850_000  # PAGE_PAPERS[0..2] -> 850000..850002
FREE_EUROPE_PMC_ID = str(_EUROPE_PMC_ID_BASE)


def _europe_pmc_result(ext_id: int, title: str, doi: str) -> dict:
    return {
        "pmid": str(ext_id),
        "title": title,
        "authorList": {"author": [{"fullName": "Ada Fixture"}]},
        "pubYear": "2026",
        "doi": doi,
        "citedByCount": 3,
    }


def _europe_pmc_handle(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    page_size = int(params["pageSize"])
    cursor_mark = params["cursorMark"]
    if page_size < len(PAPERS):  # a direct search_page() pagination test (page_size=2), same convention as PubMed
        offset = 0 if cursor_mark == "*" else int(cursor_mark)
        items = _page_slice(offset, page_size)
        results = [
            _europe_pmc_result(_PAGE_EUROPE_PMC_ID_BASE + offset + i, title, doi)
            for i, (title, doi) in enumerate(items)
        ]
        body: dict = {"hitCount": len(PAGE_PAPERS), "resultList": {"result": results}}
        next_offset = offset + page_size
        if next_offset < len(PAGE_PAPERS):
            body["nextCursorMark"] = str(next_offset)
        return httpx.Response(200, json=body)
    results = [_europe_pmc_result(_EUROPE_PMC_ID_BASE + i, title, doi) for i, (title, doi, _) in enumerate(PAPERS)]
    return httpx.Response(200, json={"hitCount": len(PAPERS), "resultList": {"result": results}})


# Zenodo is one-step like Europe PMC, but paginates with a 1-based `page` number (not an offset) and signals
# "is there more" via links.next's presence, same as zenodo.py's own search_page() -- own id bases, offset past
# Europe PMC's 840000/850000, so fixture ids never collide with any other source's.
_ZENODO_ID_BASE = 860_000       # PAPERS[0..2] -> 860000..860002
_PAGE_ZENODO_ID_BASE = 870_000  # PAGE_PAPERS[0..2] -> 870000..870002
FREE_ZENODO_ID = str(_ZENODO_ID_BASE)


def _zenodo_hit(record_id: int, title: str, doi: str, pdf_url: str | None) -> dict:
    files = [{"key": "paper.pdf", "links": {"self": pdf_url}}] if pdf_url else []
    return {
        "id": record_id,
        "doi": doi,
        "metadata": {
            "title": title,
            "creators": [{"name": "Ada Fixture"}],
            "publication_date": "2026-01-05",
            "description": "An overview of fixtures.",
        },
        "files": files,
    }


def _zenodo_handle(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    size = int(params["size"])
    if size < len(PAPERS):  # a direct search_page() pagination test (page_size=2), same convention as PubMed
        page = int(params["page"])
        offset = (page - 1) * size
        items = _page_slice(offset, size)
        hits = [
            _zenodo_hit(_PAGE_ZENODO_ID_BASE + offset + i, title, doi, None) for i, (title, doi) in enumerate(items)
        ]
        body: dict = {"hits": {"hits": hits}}
        if offset + size < len(PAGE_PAPERS):
            body["links"] = {"next": "https://zenodo.org/api/records?page=2"}
        return httpx.Response(200, json=body)
    hits = [_zenodo_hit(_ZENODO_ID_BASE + i, title, doi, pdf_url) for i, (title, doi, pdf_url) in enumerate(PAPERS)]
    return httpx.Response(200, json={"hits": {"hits": hits}})


# HAL shares PubMed/PMC's exact offset shape (0-based start/rows), just with Solr field-list values instead of
# XML -- own id bases, offset past Zenodo's 860000/870000, so fixture ids never collide with any other source's.
_HAL_ID_BASE = 880_000       # PAPERS[0..2] -> 880000..880002
_PAGE_HAL_ID_BASE = 890_000  # PAGE_PAPERS[0..2] -> 890000..890002
FREE_HAL_ID = str(_HAL_ID_BASE)


def _hal_doc(docid: int, title: str, doi: str, pdf_url: str | None) -> dict:
    return {
        "docid": str(docid),
        "title_s": [title],
        "abstract_s": ["An overview of fixtures."],
        "authFullName_s": ["Ada Fixture"],
        "doiId_s": doi,
        "producedDate_s": "2026-01-05",
        "files_s": [pdf_url] if pdf_url else [],
    }


def _hal_handle(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    rows = int(params["rows"])
    start = int(params["start"])
    if rows < len(PAPERS):  # a direct search_page() pagination test (page_size=2), same convention as PubMed
        items = _page_slice(start, rows)
        docs = [_hal_doc(_PAGE_HAL_ID_BASE + start + i, title, doi, None) for i, (title, doi) in enumerate(items)]
        return httpx.Response(200, json={"response": {"numFound": len(PAGE_PAPERS), "docs": docs}})
    docs = [_hal_doc(_HAL_ID_BASE + i, title, doi, pdf_url) for i, (title, doi, pdf_url) in enumerate(PAPERS)]
    return httpx.Response(200, json={"response": {"numFound": len(PAPERS), "docs": docs}})


# DOAJ is one-step like Zenodo, with a 1-based page cursor and a "next" key presence signal, same as
# doaj.py's own search_page() -- own id bases, offset past HAL's 880000/890000.
_DOAJ_ID_BASE = 900_000       # PAPERS[0..2] -> 900000..900002, zero-padded to doaj's own 32-hex id shape
_PAGE_DOAJ_ID_BASE = 910_000  # PAGE_PAPERS[0..2] -> 910000..910002
FREE_DOAJ_ID = f"{_DOAJ_ID_BASE:032d}"


def _doaj_article(article_id: int, title: str, doi: str, pdf_url: str | None) -> dict:
    links = [{"content_type": "pdf", "type": "fulltext", "url": pdf_url}] if pdf_url else []
    return {
        "id": f"{article_id:032d}",
        "bibjson": {
            "title": title, "year": "2026", "author": [{"name": "Ada Fixture"}],
            "identifier": [{"id": doi, "type": "doi"}], "link": links,
        },
    }


def _doaj_handle(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    page_size = int(params["pageSize"])
    page = int(params["page"])
    if page_size < len(PAPERS):  # a direct search_page() pagination test, same convention as PubMed
        offset = (page - 1) * page_size
        items = _page_slice(offset, page_size)
        results = [
            _doaj_article(_PAGE_DOAJ_ID_BASE + offset + i, title, doi, None) for i, (title, doi) in enumerate(items)
        ]
        body: dict = {"total": len(PAGE_PAPERS), "page": page, "results": results}
        if offset + page_size < len(PAGE_PAPERS):
            body["next"] = f"https://doaj.org/api/v4/search/articles/x?page={page + 1}"
        return httpx.Response(200, json=body)
    results = [_doaj_article(_DOAJ_ID_BASE + i, title, doi, pdf_url) for i, (title, doi, pdf_url) in enumerate(PAPERS)]
    return httpx.Response(200, json={"total": len(PAPERS), "page": 1, "results": results})


# OpenAIRE is one-step with a 1-based page cursor and a numFound-based "more" signal, same as
# openaire.py's own search_page() -- own id bases, offset past DOAJ's 900000/910000.
_OPENAIRE_ID_BASE = 920_000       # PAPERS[0..2] -> 920000..920002
_PAGE_OPENAIRE_ID_BASE = 930_000  # PAGE_PAPERS[0..2] -> 930000..930002
FREE_OPENAIRE_ID = f"openaire____::{_OPENAIRE_ID_BASE:032d}"


def _openaire_record(record_id: str, title: str, doi: str) -> dict:
    return {
        "id": record_id, "type": "publication", "mainTitle": title,
        "descriptions": ["An overview of fixtures."], "pids": [{"scheme": "doi", "value": doi}],
        "authors": [{"fullName": "Ada Fixture"}], "publicationDate": "2026-01-05",
        "instances": [{"urls": [f"https://doi.org/{doi}"]}],
    }


def _openaire_handle(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    page_size = int(params["pageSize"])
    page = int(params["page"])
    if page_size < len(PAPERS):  # a direct search_page() pagination test, same convention as PubMed
        offset = (page - 1) * page_size
        items = _page_slice(offset, page_size)
        results = [
            _openaire_record(f"openaire____::{_PAGE_OPENAIRE_ID_BASE + offset + i:032d}", title, doi)
            for i, (title, doi) in enumerate(items)
        ]
        body = {"header": {"numFound": len(PAGE_PAPERS), "page": page, "pageSize": page_size}, "results": results}
        return httpx.Response(200, json=body)
    results = [
        _openaire_record(f"openaire____::{_OPENAIRE_ID_BASE + i:032d}", title, doi)
        for i, (title, doi, _) in enumerate(PAPERS)
    ]
    body = {"header": {"numFound": len(PAPERS), "page": 1, "pageSize": 20}, "results": results}
    return httpx.Response(200, json=body)


@dataclass(frozen=True)
class FakeAdapter:
    """One source's fake: `host` plus a `match(path, params)` predicate decide whether this adapter answers a
    request; `handle` builds the response. `_handle` below is pure dispatch over a list of these."""

    host: str
    match: Callable[[str, httpx.QueryParams], bool]
    handle: Callable[[httpx.Request], httpx.Response]


def _dblp_handle(request: httpx.Request) -> httpx.Response:
    offset = int(request.url.params.get("f", "0"))
    size = int(request.url.params.get("h", "20"))
    hits = [{"info": {"key": f"conf/paperlab/{i}", "title": title, "doi": doi}}
            for i, (title, doi, _) in enumerate(PAPERS)][offset:offset + size]
    return httpx.Response(200, json={"result": {"status": {"@code": "200"}, "hits": {
        "@total": str(len(PAPERS)), "@sent": str(len(hits)), "hit": hits,
    }}})


def _openalex_handle(request: httpx.Request) -> httpx.Response:
    if "page" in request.url.params:
        return _openalex_search_page(request.url.params)
    if request.url.path.startswith("/works/"):
        return httpx.Response(200, json=_work(*PAPERS[0]))
    return httpx.Response(200, json={"results": [_work(*paper) for paper in PAPERS]})


def _s2_handle(request: httpx.Request) -> httpx.Response:
    s2_papers = [_s2_paper(i, *paper) for i, paper in enumerate(PAPERS)]
    path = request.url.path
    if path.endswith("/references"):
        return httpx.Response(200, json={"offset": 0, "data": [{"citedPaper": p} for p in s2_papers]})
    if path.endswith("/citations"):
        return httpx.Response(200, json={"offset": 0, "data": [{"citingPaper": s2_papers[0]}]})
    if path.startswith("/recommendations/"):
        return httpx.Response(200, json={"recommendedPapers": s2_papers})
    if path == "/graph/v1/paper/search/match":
        return httpx.Response(200, json={"data": [{"paperId": "f" * 40}]})
    if path == "/graph/v1/paper/search":
        return _s2_search_page(request.url.params)
    if path == "/graph/v1/paper/batch":
        return httpx.Response(200, json=[None] * len(json.loads(request.read())["ids"]))
    return httpx.Response(200, json=s2_papers[0])


def _crossref_handle(request: httpx.Request) -> httpx.Response:
    if "offset" in request.url.params:
        return _crossref_search_page(request.url.params)
    if request.url.path.startswith("/works/"):
        return httpx.Response(200, json={"message": _crossref_item(*PAPERS[0])})
    return httpx.Response(200, json={"message": {"items": [_crossref_item(*paper) for paper in PAPERS]}})


def _arxiv_handle(request: httpx.Request) -> httpx.Response:
    if "start" in request.url.params:
        return httpx.Response(
            200, text=_arxiv_search_page_feed(request.url.params), headers={"content-type": "application/atom+xml"}
        )
    return httpx.Response(200, text=_arxiv_feed(), headers={"content-type": "application/atom+xml"})


def _core_handle(request: httpx.Request) -> httpx.Response:
    if "offset" in request.url.params:
        return _core_search_page(request.url.params)
    return httpx.Response(200, json={"results": [_core_work()]})


def _unpaywall_handle(request: httpx.Request) -> httpx.Response:
    pdf_url = next((url for _, doi, url in PAPERS if request.url.path == f"/v2/{doi}"), None)
    if pdf_url:
        return httpx.Response(200, json={"best_oa_location": {"url_for_pdf": pdf_url}, "oa_locations": []})
    return httpx.Response(404, json={"error": f"the discovery fake has no {request.url}"})


def _pdf_handle(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/paper.pdf":
        return httpx.Response(200, content=pdf_bytes(), headers={"content-type": "application/pdf"})
    return httpx.Response(200, text="<html><body>A landing page, not a PDF</body></html>")


ADAPTERS: tuple[FakeAdapter, ...] = (
    FakeAdapter("dblp.org", lambda path, params: path == "/search/publ/api", _dblp_handle),
    FakeAdapter(
        "api.openalex.org",
        lambda path, params: path == "/works" or path.startswith("/works/"),
        _openalex_handle,
    ),
    FakeAdapter("api.semanticscholar.org", lambda path, params: True, _s2_handle),
    FakeAdapter(
        "api.crossref.org",
        lambda path, params: path == "/works" or path.startswith("/works/"),
        _crossref_handle,
    ),
    FakeAdapter("export.arxiv.org", lambda path, params: path == "/api/query", _arxiv_handle),
    FakeAdapter("api.core.ac.uk", lambda path, params: path == "/v3/search/works/", _core_handle),
    FakeAdapter("api.unpaywall.org", lambda path, params: True, _unpaywall_handle),
    FakeAdapter(
        "eutils.ncbi.nlm.nih.gov",
        # PMC shares this exact host with PubMed (same eutils, same esearch.fcgi/efetch.fcgi paths) -- the two
        # FakeAdapter entries below both match on this host, and dispatch between them is each one's own `db=`
        # check, not a path difference, mirroring how pubmed.py/pmc.py themselves only differ by that one param.
        lambda path, params: (path.endswith("esearch.fcgi") or path.endswith("efetch.fcgi"))
        and params.get("db") == "pubmed",
        _pubmed_handle,
    ),
    FakeAdapter(
        "eutils.ncbi.nlm.nih.gov",
        lambda path, params: (path.endswith("esearch.fcgi") or path.endswith("efetch.fcgi"))
        and params.get("db") == "pmc",
        _pmc_handle,
    ),
    FakeAdapter("www.ebi.ac.uk", lambda path, params: path.endswith("/search"), _europe_pmc_handle),
    FakeAdapter("zenodo.org", lambda path, params: path == "/api/records", _zenodo_handle),
    FakeAdapter("api.archives-ouvertes.fr", lambda path, params: path == "/search/", _hal_handle),
    FakeAdapter("doaj.org", lambda path, params: path.startswith("/api/search/articles/"), _doaj_handle),
    FakeAdapter("api.openaire.eu", lambda path, params: path == "/graph/v3/research-products", _openaire_handle),
    FakeAdapter("pdf.paperlab.test", lambda path, params: True, _pdf_handle),
)  # fmt: skip


def _handle(request: httpx.Request) -> httpx.Response:
    for adapter in ADAPTERS:
        if request.url.host == adapter.host and adapter.match(request.url.path, request.url.params):
            return adapter.handle(request)
    return httpx.Response(404, json={"error": f"the discovery fake has no {request.url}"})


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handle)
