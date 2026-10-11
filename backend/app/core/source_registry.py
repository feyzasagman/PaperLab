"""Every paper source PaperLab knows about, in one place: its identity, whether it takes a key, its default
on/off state, how to build its httpx client, and how Find Papers/Similar (`ask`) and Workspace Search
(`page`/`mapper`) each use it (Phase 0). Adding a new source means adding one SourceSpec here — not hand-editing
paper_sources.py, the paper_sources/workspace_search schemas, discovery.py's ASKS, and workspace_search.py's
PAGE_FUNCS/MAPPERS separately, which is exactly the shotgun-surgery pattern that once let a source be present in
one dict and missing from another (the C1 bug).

REGISTRY's own tuple order is D73's merge trust order — most trusted first — preserved byte-for-byte from today's
app.core.paper_sources.SOURCES. Every derived constant below keeps that order.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.candidates import (
    Candidate,
    from_acm_dl,
    from_arxiv,
    from_biorxiv,
    from_core,
    from_crossref,
    from_doaj,
    from_europe_pmc,
    from_hal,
    from_iacr_eprint,
    from_medrxiv,
    from_openaire,
    from_pmc,
    from_pubmed,
    from_s2,
    from_ssrn,
    from_work,
    from_zenodo,
)
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
    unpaywall,
    zenodo,
)

# How many results Find Papers/Similar asks each source for, per query (was discovery.py's PER_SOURCE).
PER_SOURCE = 10

Ask = Callable[[httpx.AsyncClient, str, str], Awaitable[list[Candidate]]]
# int | str: every source but Europe PMC uses a numeric offset; Europe PMC's cursorMark is an opaque
# string token (Batch 2) -- this widening is a type hint only (no static type checker runs in this
# project), so no existing provider's own narrower `cursor: int` signature needs to change.
PageFunc = Callable[[httpx.AsyncClient, str, int, int | str], Awaitable[tuple[list[Any], int | str | None]]]
Mapper = Callable[[Any], Candidate | None]
NewClient = Callable[..., httpx.AsyncClient]


async def _openalex(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    if kind == "title":
        return [from_work(w) for w in await openalex.search_works(http, value, per_page=PER_SOURCE)]
    work = await openalex.get_work(http, f"doi:{value}" if kind == "doi" else value)
    return [from_work(work)] if work else []


async def _crossref(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    items = (
        await crossref.search(http, value, PER_SOURCE) if kind == "title" else [await crossref.get_work(http, value)]
    )
    return [candidate for item in items if item and (candidate := from_crossref(item))]


async def _semantic_scholar(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    # OpenAlex's arXiv location filter returned a wrongly merged record for BERT; Semantic Scholar's lookup didn't.
    paper = await semantic_scholar.get_paper(http, f"arXiv:{value}")
    return [from_s2(paper)] if paper else []


async def _arxiv(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    entries = await arxiv.search(http, value, PER_SOURCE) if kind == "title" else [await arxiv.get(http, value)]
    return [from_arxiv(entry) for entry in entries if entry]


async def _core(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_core(work) for work in await core_ac.search(http, value, PER_SOURCE)]


async def _pubmed(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_pubmed(entry) for entry in await pubmed.search(http, value, PER_SOURCE)]


async def _pmc(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_pmc(entry) for entry in await pmc.search(http, value, PER_SOURCE)]


async def _europe_pmc(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_europe_pmc(entry) for entry in await europe_pmc.search(http, value, PER_SOURCE)]


async def _zenodo(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_zenodo(entry) for entry in await zenodo.search(http, value, PER_SOURCE)]


async def _hal(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_hal(entry) for entry in await hal.search(http, value, PER_SOURCE)]


async def _acm_dl(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    items = await acm_dl.search(http, value, PER_SOURCE)
    return [candidate for item in items if item and (candidate := from_acm_dl(item))]


async def _ssrn(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_ssrn(work) for work in await ssrn.search(http, value, PER_SOURCE)]


async def _doaj(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    return [from_doaj(entry) for entry in await doaj.search(http, value, PER_SOURCE)]


async def _openaire(http: httpx.AsyncClient, kind: str, value: str) -> list[Candidate]:
    items = await openaire.search(http, value, PER_SOURCE)
    return [candidate for item in items if item and (candidate := from_openaire(item))]


@dataclass(frozen=True)
class SourceSpec:
    """One paper source. `ask_kinds` are the classify_query() kinds (app/core/discovery.py) this source answers
    for Find Papers/Similar. `is_discovery_source` is whether it participates in Workspace Search's paginated
    search_batch at all — Unpaywall never does (DOI-only PDF enrichment, applied post-merge, never a search
    source: app/core/paper_sources.py's own comment, "Unpaywall only adds PDF links")."""

    id: str
    name: str
    keyed: bool
    enabled_by_default: bool
    is_discovery_source: bool
    new_client: NewClient
    ask: Ask | None = None
    ask_kinds: tuple[str, ...] = ()
    page: PageFunc | None = None
    mapper: Mapper | None = None
    page_size: int = 20
    starting_cursor: int | str = 0
    key_source: str | None = None  # None: use this source's own id for the key lookup. Set when a source
    # rides a different source's client/key entirely (e.g. SSRN reuses OpenAlex's own metered client and
    # key) -- `build_providers` looks the key up under `key_source` instead of `id` when this is set.


REGISTRY: tuple[SourceSpec, ...] = (
    SourceSpec(
        id="openalex", name="OpenAlex", keyed=True, enabled_by_default=False, is_discovery_source=True,
        new_client=openalex.new_client, ask=_openalex, ask_kinds=("title", "doi", "openalex"),
        page=openalex.search_page, mapper=from_work, page_size=100, starting_cursor=1,
    ),
    SourceSpec(
        id="crossref", name="Crossref", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=crossref.new_client, ask=_crossref, ask_kinds=("title", "doi"),
        page=crossref.search_page, mapper=from_crossref, page_size=30,
    ),
    SourceSpec(
        id="semantic_scholar", name="Semantic Scholar", keyed=True, enabled_by_default=True,
        is_discovery_source=True, new_client=semantic_scholar.new_client, ask=_semantic_scholar,
        ask_kinds=("arxiv",), page=semantic_scholar.search_page, mapper=from_s2, page_size=75,
    ),
    SourceSpec(
        id="arxiv", name="arXiv", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=arxiv.new_client, ask=_arxiv, ask_kinds=("title", "arxiv"),
        page=arxiv.search_page, mapper=from_arxiv, page_size=20,
    ),
    SourceSpec(
        id="core", name="CORE", keyed=True, enabled_by_default=True, is_discovery_source=True,
        new_client=core_ac.new_client, ask=_core, ask_kinds=("title",),
        page=core_ac.search_page, mapper=from_core, page_size=20,
    ),
    SourceSpec(
        id="unpaywall", name="Unpaywall", keyed=False, enabled_by_default=True, is_discovery_source=False,
        new_client=unpaywall.new_client,
    ),
    SourceSpec(
        id="pubmed", name="PubMed", keyed=True, enabled_by_default=True, is_discovery_source=True,
        new_client=pubmed.new_client, ask=_pubmed, ask_kinds=("title",),
        page=pubmed.search_page, mapper=from_pubmed, page_size=20,
    ),
    SourceSpec(
        id="pmc", name="PMC", keyed=True, enabled_by_default=True, is_discovery_source=True,
        new_client=pmc.new_client, ask=_pmc, ask_kinds=("title",),
        page=pmc.search_page, mapper=from_pmc, page_size=20,
    ),
    SourceSpec(
        id="europe_pmc", name="Europe PMC", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=europe_pmc.new_client, ask=_europe_pmc, ask_kinds=("title",),
        page=europe_pmc.search_page, mapper=from_europe_pmc, page_size=25, starting_cursor="*",
    ),
    SourceSpec(
        id="zenodo", name="Zenodo", keyed=True, enabled_by_default=True, is_discovery_source=True,
        new_client=zenodo.new_client, ask=_zenodo, ask_kinds=("title",),
        page=zenodo.search_page, mapper=from_zenodo, page_size=25, starting_cursor=1,
    ),
    SourceSpec(
        id="hal", name="HAL", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=hal.new_client, ask=_hal, ask_kinds=("title",),
        page=hal.search_page, mapper=from_hal, page_size=25,
    ),
    SourceSpec(
        id="acm_dl", name="ACM DL", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=crossref.new_client, ask=_acm_dl, ask_kinds=("title",),
        page=acm_dl.search_page, mapper=from_acm_dl, page_size=30,
    ),
    SourceSpec(
        id="ssrn", name="SSRN", keyed=False, enabled_by_default=False, is_discovery_source=True,
        new_client=openalex.new_client, ask=_ssrn, ask_kinds=("title",),
        page=ssrn.search_page, mapper=from_ssrn, page_size=25, key_source="openalex",
    ),
    SourceSpec(
        id="doaj", name="DOAJ", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=doaj.new_client, ask=_doaj, ask_kinds=("title",),
        page=doaj.search_page, mapper=from_doaj, page_size=100, starting_cursor=1,
    ),
    SourceSpec(
        id="openaire", name="OpenAIRE", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=openaire.new_client, ask=_openaire, ask_kinds=("title",),
        page=openaire.search_page, mapper=from_openaire, page_size=20, starting_cursor=1,
    ),
    SourceSpec(
        id="iacr_eprint", name="IACR ePrint", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=iacr_eprint.new_client, page=iacr_eprint.search_page, mapper=from_iacr_eprint, page_size=50,
    ),
    SourceSpec(
        id="biorxiv", name="bioRxiv", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=biorxiv.new_client, page=biorxiv.search_page_biorxiv, mapper=from_biorxiv, page_size=30,
    ),
    SourceSpec(
        id="medrxiv", name="medRxiv", keyed=False, enabled_by_default=True, is_discovery_source=True,
        new_client=biorxiv.new_client, page=biorxiv.search_page_medrxiv, mapper=from_medrxiv, page_size=30,
    ),
)

BY_ID: dict[str, SourceSpec] = {spec.id: spec for spec in REGISTRY}
SOURCE_IDS: tuple[str, ...] = tuple(spec.id for spec in REGISTRY)
DISCOVERY_SOURCE_IDS: tuple[str, ...] = tuple(spec.id for spec in REGISTRY if spec.is_discovery_source)
NAMES: dict[str, str] = {spec.id: spec.name for spec in REGISTRY}
KEYED_IDS: tuple[str, ...] = tuple(spec.id for spec in REGISTRY if spec.keyed)
ENABLED_BY_DEFAULT: dict[str, bool] = {spec.id: spec.enabled_by_default for spec in REGISTRY}
ASKS: dict[str, dict[str, Ask]] = {
    kind: {spec.id: spec.ask for spec in REGISTRY if spec.ask and kind in spec.ask_kinds}
    for kind in ("title", "doi", "arxiv", "openalex")
}
PAGE_SIZE_BY_SOURCE: dict[str, int] = {spec.id: spec.page_size for spec in REGISTRY if spec.is_discovery_source}
PAGE_FUNCS: dict[str, PageFunc] = {spec.id: spec.page for spec in REGISTRY if spec.page}
MAPPERS: dict[str, Mapper] = {spec.id: spec.mapper for spec in REGISTRY if spec.mapper}
STARTING_CURSOR_VALUE: dict[str, int | str] = {
    spec.id: spec.starting_cursor for spec in REGISTRY if spec.starting_cursor
}
