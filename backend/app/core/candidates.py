"""Papers found outside the library: one candidate per source record, merged into one list (M19, M19.5).

- PDF URLs: arXiv by ID first, then each record's links in order; landing pages are never listed (D68).
- Merging (D73): records sharing a DOI, arXiv ID, OpenAlex ID, Semantic Scholar ID or CORE ID are one paper, and so
  are records with the same title and a shared author surname. Fields come from the most trusted record that has them.
"""

import html
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.core.enrichment import ARXIV_DOI_PREFIX, abstract_text, normalize_doi, short_id
from app.core.source_types import source_type_of

# CandidateIn (app/schemas/discovery.py) rejects a candidate over either limit with 422; capping here first means
# the Add button never shows for a candidate its own endpoint would then refuse.
MAX_PDF_URLS = 10
MAX_AUTHORS = 500
# Crossref's title search also returns songs, datasets and peer reviews ("All You Need Is LSD" is type "other").
CROSSREF_PAPER_TYPES = frozenset(
    {"journal-article", "proceedings-article", "posted-content", "book-chapter", "book", "monograph", "report",
     "dissertation"}
)  # fmt: skip

ARXIV_ID = r"\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7}"
_ARXIV_URL = re.compile(rf"arxiv\.org/(?:abs|pdf)/({ARXIV_ID})", re.IGNORECASE)
ARXIV_VERSION = re.compile(r"v\d+$")


@dataclass(frozen=True)
class Candidate:
    """A paper found outside the library. `sources` lists every source that found it, most trusted first; `paper_id`
    is set when the library already holds it. `external_ids` (Phase 0b) is keyed by the registry's own source ids
    ("openalex", "semantic_scholar", "arxiv", "core", "pubmed") — a source with no id for this paper has no key,
    never a key mapped to None."""

    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    cited_by_count: int | None = None
    abstract: str | None = None
    pdf_urls: list[str] = field(default_factory=list)
    sources: tuple[str, ...] = ()
    paper_id: uuid.UUID | None = None
    # M59 hit triage: what kind of source this is and, for a preprint with a published version, where it appeared.
    source_type: str | None = None
    published_doi: str | None = None
    published_venue: str | None = None


def arxiv_from_doi(doi: str | None) -> str | None:
    return doi.removeprefix(ARXIV_DOI_PREFIX) if doi and doi.startswith(ARXIV_DOI_PREFIX) else None


def _arxiv_from_url(url: str | None) -> str | None:
    match = _ARXIV_URL.search(url or "")
    return match.group(1).lower() if match else None


def ordered_pdf_urls(arxiv_id: str | None, *links: str | None) -> list[str]:
    """arXiv first, then `links` in order: deduplicated, http(s) only, capped at MAX_PDF_URLS (D68)."""
    urls = [f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None, *links]
    deduped = dict.fromkeys(url for url in urls if url and url.startswith(("http://", "https://")))
    return list(deduped)[:MAX_PDF_URLS]


S2_TYPES = {
    "JournalArticle": "journal_article", "Conference": "proceedings", "Review": "journal_article",
    "Book": "book", "BookSection": "chapter",
}  # fmt: skip


PREPRINT_SERVERS = re.compile(r"arxiv|biorxiv|medrxiv|ssrn|research square|preprints\.org", re.IGNORECASE)


def s2_triage(paper: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(source_type, published_doi, published_venue) from a Semantic Scholar paper. S2 often names the preprint
    server as the "journal" of a JournalArticle: that is no journal. Such a paper is a preprint; an arXiv paper
    that also has a non-arXiv DOI gives that DOI as the published version's."""
    ids = paper.get("externalIds") or {}
    types = paper.get("publicationTypes") or []

    def real(name: str | None) -> str | None:
        return None if not name or PREPRINT_SERVERS.search(name) else name

    journal, venue = (paper.get("journal") or {}).get("name"), paper.get("venue")
    named_server = any(n and PREPRINT_SERVERS.search(n) for n in (journal, venue))
    source_type = next((S2_TYPES[t] for t in types if t in S2_TYPES), None)
    if set(types) <= {"JournalArticle"} and not real(journal) and (named_server or ids.get("ArXiv")):
        source_type = "preprint"
    doi = normalize_doi(ids.get("DOI") or "") if ids.get("ArXiv") else None
    if doi and doi.startswith(ARXIV_DOI_PREFIX):
        doi = None
    return source_type, doi, None if source_type == "preprint" else real(journal) or real(venue)


def arxiv_triage(entry: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(source_type, published_doi, published_venue) from an arXiv entry: a journal_ref means it was published."""
    journal_ref = (entry.get("journal_ref") or "").strip() or None
    if not journal_ref:
        return "preprint", None, None
    doi = normalize_doi(entry.get("doi") or "")
    return "journal_article", None if doi and doi.startswith(ARXIV_DOI_PREFIX) else doi, journal_ref


def from_work(work: dict[str, Any]) -> Candidate:
    doi = (work.get("doi") or "").removeprefix("https://doi.org/").lower() or None
    best = work.get("best_oa_location") or {}
    locations = work.get("locations") or []
    location_urls = [
        url for place in [best, *locations] for url in (place.get("landing_page_url"), place.get("pdf_url"))
    ]
    arxiv_id = arxiv_from_doi(doi) or next(filter(None, map(_arxiv_from_url, location_urls)), None)
    source = (work.get("primary_location") or {}).get("source") or {}
    authors = [a["author"]["display_name"] for a in work.get("authorships") or [] if a.get("author")]
    external_ids = {k: v for k, v in {"openalex": short_id(work.get("id")), "arxiv": arxiv_id}.items() if v}
    source_type = source_type_of(work.get("type"), doi)
    if PREPRINT_SERVERS.search(source.get("display_name") or ""):
        source_type = "preprint"  # hosted on a preprint server, whatever OpenAlex's type says
    return Candidate(
        title=work.get("title") or "Untitled",
        authors=authors[:MAX_AUTHORS],
        year=work.get("publication_year"),
        venue=source.get("display_name"),
        doi=doi,
        external_ids=external_ids,
        cited_by_count=work.get("cited_by_count"),
        abstract=abstract_text(work.get("abstract_inverted_index")),
        pdf_urls=ordered_pdf_urls(arxiv_id, best.get("pdf_url"), *(place.get("pdf_url") for place in locations)),
        sources=("openalex",),
        source_type=source_type,
        published_venue=None if source_type == "preprint" else source.get("display_name"),
    )


def from_s2(paper: dict[str, Any]) -> Candidate:
    ids = paper.get("externalIds") or {}
    doi = (ids.get("DOI") or "").lower() or None
    arxiv_id = (ids.get("ArXiv") or "").lower() or arxiv_from_doi(doi)
    external_ids = {k: v for k, v in {"semantic_scholar": paper.get("paperId"), "arxiv": arxiv_id}.items() if v}
    source_type, published_doi, published_venue = s2_triage(paper)
    return Candidate(
        title=paper.get("title") or "Untitled",
        authors=[a["name"] for a in paper.get("authors") or [] if a.get("name")][:MAX_AUTHORS],
        year=paper.get("year"),
        venue=paper.get("venue") or None,
        doi=doi,
        external_ids=external_ids,
        cited_by_count=paper.get("citationCount"),
        abstract=paper.get("abstract") or None,
        pdf_urls=ordered_pdf_urls(arxiv_id, (paper.get("openAccessPdf") or {}).get("url")),
        sources=("semantic_scholar",),
        source_type=source_type,
        published_doi=published_doi,
        published_venue=published_venue,
    )


def with_s2(candidate: Candidate, paper: dict[str, Any] | None) -> Candidate:
    """A candidate with Semantic Scholar's arXiv ID and free PDF link added (D70). A lookup, so no new source."""
    if paper is None:
        return candidate
    found = from_s2(paper)
    arxiv_id = candidate.external_ids.get("arxiv") or found.external_ids.get("arxiv")
    external_ids = {**candidate.external_ids, "semantic_scholar": found.external_ids.get("semantic_scholar")}
    if arxiv_id:
        external_ids["arxiv"] = arxiv_id
    return replace(
        candidate,
        external_ids={k: v for k, v in external_ids.items() if v},
        pdf_urls=ordered_pdf_urls(arxiv_id, *found.pdf_urls, *candidate.pdf_urls),
    )


def from_dblp(entry: Mapping[str, Any]) -> Candidate:
    """DBLP supplies metadata and landing pages, never PDF links."""
    authors = (entry.get("authors") or {}).get("author") or []
    if not isinstance(authors, list):
        authors = [authors]
    names = [a.get("text", "") if isinstance(a, dict) else a for a in authors]
    venue = entry.get("venue") or None
    if isinstance(venue, list):
        venue = venue[0] if venue else None
    year = str(entry.get("year") or "")
    doi = (entry.get("doi") or "").removeprefix("https://doi.org/").strip().lower() or None
    links = entry.get("ee") or []
    if not isinstance(links, list):
        links = [links]
    arxiv_id = arxiv_from_doi(doi) or next(
        (found for link in links if isinstance(link, str) and (found := _arxiv_from_url(link))), None
    )
    arxiv_id = ARXIV_VERSION.sub("", arxiv_id) if arxiv_id else None
    ids = {k: v for k, v in {"dblp": entry.get("key"), "arxiv": arxiv_id}.items() if v}
    return Candidate(
        title=html.unescape(entry.get("title") or "Untitled").rstrip("."),
        authors=[re.sub(r"\s\d{4}$", "", html.unescape(n)) for n in names if isinstance(n, str) and n][:MAX_AUTHORS],
        year=int(year) if year.isdigit() and len(year) == 4 else None,
        venue=venue, doi=doi, external_ids=ids,
        pdf_urls=ordered_pdf_urls(arxiv_id), sources=("dblp",),
        source_type={
            "Informal and Other Publications": "preprint",
            "Conference and Workshop Papers": "proceedings",
            "Journal Articles": "journal_article",
        }.get(entry.get("type")),
    )


def from_arxiv(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/arxiv.py: its arXiv ID already has no version, and is always present (a parsed
    entry with no id is filtered out by providers/arxiv.py's own _entry())."""
    source_type, published_doi, published_venue = arxiv_triage(entry)
    return Candidate(
        title=entry["title"] or "Untitled",
        authors=list(entry["authors"])[:MAX_AUTHORS],
        year=entry["year"],
        doi=entry["doi"],
        external_ids={"arxiv": entry["arxiv_id"]},
        abstract=entry.get("abstract"),
        pdf_urls=ordered_pdf_urls(entry["arxiv_id"]),
        sources=("arxiv",),
        source_type=source_type,
        published_doi=published_doi,
        published_venue=published_venue,
    )


def from_crossref(item: Mapping[str, Any]) -> Candidate | None:
    """None for a record that isn't a paper. Crossref lists no free PDFs: its links are for text mining (D68).
    No abstract either: Crossref's `abstract` field, when present, is raw JATS XML markup, not plain text —
    left unset here rather than surfacing tags to the reader."""
    if item.get("type") not in CROSSREF_PAPER_TYPES:
        return None
    doi = (item.get("DOI") or "").lower() or None
    names = [
        " ".join(filter(None, (a.get("given"), a.get("family")))) or a.get("name") for a in item.get("author") or []
    ]
    [year, *_] = ((item.get("issued") or {}).get("date-parts") or [[None]])[0] or [None]
    arxiv_id = arxiv_from_doi(doi)
    venue = next(iter(item.get("container-title") or []), None)
    source_type = source_type_of(item.get("type"), doi, crossref=True)
    return Candidate(
        title=next(iter(item.get("title") or []), None) or "Untitled",
        authors=[name for name in names if name][:MAX_AUTHORS],
        year=year,
        venue=venue,
        doi=doi,
        external_ids={"arxiv": arxiv_id} if arxiv_id else {},
        cited_by_count=item.get("is-referenced-by-count"),
        pdf_urls=ordered_pdf_urls(arxiv_id),
        sources=("crossref",),
        source_type=source_type,
        published_venue=None if source_type == "preprint" else venue,
    )


def from_core(work: Mapping[str, Any]) -> Candidate:
    """Only `downloadUrl` is a PDF link: CORE's other full-text URLs include landing pages (D68). Its citation count
    is left out, since CORE counted 0 for a paper cited 100,000 times."""
    doi = (work.get("doi") or "").lower() or None
    arxiv_id = ARXIV_VERSION.sub("", (work.get("arxivId") or "").lower()) or arxiv_from_doi(doi)
    core_id = str(work["id"]) if work.get("id") is not None else None
    external_ids = {k: v for k, v in {"arxiv": arxiv_id, "core": core_id}.items() if v}
    return Candidate(
        title=" ".join((work.get("title") or "").split()) or "Untitled",
        authors=[a["name"] for a in work.get("authors") or [] if a.get("name")][:MAX_AUTHORS],
        year=work.get("yearPublished"),
        doi=doi,
        external_ids=external_ids,
        abstract=work.get("abstract") or None,
        pdf_urls=ordered_pdf_urls(arxiv_id, work.get("downloadUrl")),
        sources=("core",),
    )


def from_pubmed(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/pubmed.py. PubMed never lists a free PDF (PMC's own job, a later milestone)
    and never a citation count (not a MEDLINE field)."""
    doi = entry.get("doi")
    arxiv_id = arxiv_from_doi(doi)
    external_ids = {k: v for k, v in {"pubmed": entry["pmid"], "arxiv": arxiv_id}.items() if v}
    return Candidate(
        title=entry["title"] or "Untitled",
        authors=list(entry["authors"])[:MAX_AUTHORS],
        year=entry["year"],
        doi=doi,
        external_ids=external_ids,
        abstract=entry.get("abstract"),
        pdf_urls=ordered_pdf_urls(arxiv_id),
        sources=("pubmed",),
    )


def from_pmc(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/pmc.py. No free PDF: PMC's own OA Web Service that used to provide one is
    confirmed shut down, and the obvious constructed path is a JS proof-of-work interstitial, not a file
    (probed live 2026-10-09)."""
    doi = entry.get("doi")
    arxiv_id = arxiv_from_doi(doi)
    external_ids = {
        k: v for k, v in {"pmc": entry["pmcid"], "pubmed": entry.get("pmid"), "arxiv": arxiv_id}.items() if v
    }
    return Candidate(
        title=entry["title"] or "Untitled",
        authors=list(entry["authors"])[:MAX_AUTHORS],
        year=entry["year"],
        doi=doi,
        external_ids=external_ids,
        abstract=entry.get("abstract"),
        sources=("pmc",),
    )


def from_europe_pmc(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/europe_pmc.py. Reuses PubMed's and PMC's own external_ids keys ("pubmed",
    "pmc") rather than inventing a third "europe_pmc" id kind -- Europe PMC never natively owns an
    identifier, it aggregates records PubMed/PMC already key, so a paper it finds dedupes against the
    same paper found via PubMed or PMC automatically, with no extra matching code. No free PDF: the
    website's own direct PDF link is confirmed behind a Cloudflare bot challenge on every probe
    (2026-10-09), not a file this module can reliably fetch."""
    doi = entry.get("doi")
    arxiv_id = arxiv_from_doi(doi)
    external_ids = {
        k: v for k, v in {"pubmed": entry.get("pmid"), "pmc": entry.get("pmcid"), "arxiv": arxiv_id}.items() if v
    }
    return Candidate(
        title=entry["title"] or "Untitled",
        authors=list(entry["authors"])[:MAX_AUTHORS],
        year=entry["year"],
        doi=doi,
        external_ids=external_ids,
        cited_by_count=entry.get("cited_by_count"),
        abstract=entry.get("abstract"),
        sources=("europe_pmc",),
    )


def from_zenodo(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/zenodo.py. Direct PDF links are real (confirmed live 2026-10-09), unlike
    Europe PMC's own equivalent claim, which turned out to be Cloudflare-blocked."""
    doi = entry.get("doi")
    arxiv_id = arxiv_from_doi(doi)
    external_ids = {k: v for k, v in {"zenodo": entry["id"], "arxiv": arxiv_id}.items() if v}
    return Candidate(
        title=entry["title"] or "Untitled",
        authors=list(entry["authors"])[:MAX_AUTHORS],
        year=entry["year"],
        doi=doi,
        external_ids=external_ids,
        abstract=entry.get("abstract"),
        pdf_urls=ordered_pdf_urls(arxiv_id, entry.get("pdf_url")),
        sources=("zenodo",),
    )


def from_hal(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/hal.py. Direct PDF links are real (confirmed live 2026-10-09) but not
    every record has a file -- hal.py's own entry["pdf_url"] is already None when absent."""
    doi = entry.get("doi")
    arxiv_id = arxiv_from_doi(doi)
    external_ids = {k: v for k, v in {"hal": str(entry["docid"]), "arxiv": arxiv_id}.items() if v}
    return Candidate(
        title=entry["title"] or "Untitled",
        authors=list(entry["authors"])[:MAX_AUTHORS],
        year=entry["year"],
        doi=doi,
        external_ids=external_ids,
        abstract=entry.get("abstract"),
        pdf_urls=ordered_pdf_urls(arxiv_id, entry.get("pdf_url")),
        sources=("hal",),
    )


def from_acm_dl(item: Mapping[str, Any]) -> Candidate | None:
    """ACM DL records, found via Crossref filtered to the 10.1145 DOI prefix (confirmed live to be solely
    ACM's own). An ACM DL record IS a Crossref record -- just a narrower slice of the same data -- so
    this reuses from_crossref's own type filter, field reading and "no PDF" rule verbatim; only the
    sources tag differs."""
    candidate = from_crossref(item)
    return replace(candidate, sources=("acm_dl",)) if candidate else None


def from_ssrn(work: Mapping[str, Any]) -> Candidate:
    """SSRN records, found via OpenAlex filtered to its own SSRN source id (confirmed live). An SSRN
    record IS an OpenAlex work -- just a narrower slice of the same data -- so this reuses from_work's
    own field reading verbatim; only the sources tag differs. OpenAlex's pdf_url fields are consistently
    null for SSRN even when flagged open-access (confirmed live, 2026-10-10): only a landing-page link is
    ever present, and from_work already never reads that field, so pdf_urls comes back empty here with no
    extra code."""
    return replace(from_work(work), sources=("ssrn",))


_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")


def _clean_markup(text: str | None) -> str | None:
    """Strips HTML/JATS tags and unescapes entities, the same pattern zenodo.py's and europe_pmc.py's own
    private _clean() already use -- DOAJ and OpenAIRE both confirmed live to carry raw markup in their
    abstract text (ponytail: a fourth near-identical copy; extract a shared helper if a fifth source ever
    needs this too, not before)."""
    cleaned = " ".join(_TAG.sub("", html.unescape(text or "")).split())
    return cleaned or None


def from_doaj(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/doaj.py. Not every article carries a DOI (confirmed live: some only have
    an ISSN) -- doaj's own native id becomes this source's own external_ids key. Only a link whose
    content_type is "pdf" (case-insensitive) counts as a real PDF; DOAJ's own content_type field is
    otherwise messy and inconsistent (confirmed live), and an HTML landing-page link must never be
    mistaken for one."""
    bibjson = entry.get("bibjson") or {}
    doi = next(
        (ident["id"].lower() for ident in bibjson.get("identifier") or [] if ident.get("type") == "doi"), None
    )
    arxiv_id = arxiv_from_doi(doi)
    pdf_url = next(
        (link["url"] for link in bibjson.get("link") or [] if (link.get("content_type") or "").lower() == "pdf"),
        None,
    )
    year_str = bibjson.get("year") or ""
    return Candidate(
        title=bibjson.get("title") or "Untitled",
        authors=[a["name"] for a in bibjson.get("author") or [] if a.get("name")][:MAX_AUTHORS],
        year=int(year_str) if year_str.isdigit() else None,
        doi=doi,
        external_ids={"doaj": entry["id"]},
        abstract=_clean_markup(bibjson.get("abstract")),
        pdf_urls=ordered_pdf_urls(arxiv_id, pdf_url),
        sources=("doaj",),
    )


def from_openaire(entry: Mapping[str, Any]) -> Candidate | None:
    """An entry from providers/openaire.py. None for a record that isn't a publication -- search()/
    search_page() already send type=publication on every request (Review Focus #3), so this is defense
    in depth for the rare record that slips through anyway, the same role from_crossref's own type
    filter plays for ACM DL. Never a PDF: instances[].urls are DOI-resolver landing-page links,
    confirmed live to never be a direct file, matching crossref.py's own "no PDF" rule. OpenAIRE's own
    native id (a "<tag>::<32-hex>" string) becomes this source's own external_ids key."""
    if entry.get("type") != "publication":
        return None
    doi = next((pid["value"].lower() for pid in entry.get("pids") or [] if pid.get("scheme") == "doi"), None)
    arxiv_id = arxiv_from_doi(doi)
    descriptions = entry.get("descriptions") or []
    pub_date = entry.get("publicationDate") or ""
    year = int(pub_date[:4]) if pub_date[:4].isdigit() else None
    return Candidate(
        title=entry.get("mainTitle") or "Untitled",
        authors=[a["fullName"] for a in entry.get("authors") or [] if a.get("fullName")][:MAX_AUTHORS],
        year=year,
        doi=doi,
        external_ids={"openaire": entry["id"]},
        abstract=_clean_markup(descriptions[0]) if descriptions else None,
        pdf_urls=ordered_pdf_urls(arxiv_id),
        sources=("openaire",),
    )


def from_iacr_eprint(entry: Mapping[str, Any]) -> Candidate:
    """An entry from providers/iacr_eprint.py's own fetch_page(). The bare eprint id (e.g. "2026/2368")
    is extracted from the OAI identifier ("oai:eprint.iacr.org:2026/2368") and becomes this source's own
    external_ids key -- most eprints have no DOI at all. `year` comes from the id's own 4-digit year
    prefix, not the OAI `datestamp` -- confirmed live, `datestamp` is the record's last-MODIFIED time, so
    a revised eprint's datestamp can land a year after its actual publication year (roughly a quarter of
    real harvested records). The id's year prefix is always present and always valid: the id format is
    already enforced elsewhere as ^\\d{4}/\\d{1,6}$. Never a PDF: constructing one
    (https://eprint.iacr.org/<id>.pdf) is deliberately not attempted here; this plan's own scope is
    getting IACR searchable, not fetching its files."""
    eprint_id = entry["identifier"].removeprefix("oai:eprint.iacr.org:")
    return Candidate(
        title=entry.get("title") or "Untitled",
        authors=list(entry.get("creators") or [])[:MAX_AUTHORS],
        year=int(eprint_id[:4]) if eprint_id[:4].isdigit() else None,
        external_ids={"iacr_eprint": eprint_id},
        abstract=entry.get("description") or None,
        sources=("iacr_eprint",),
    )


def from_biorxiv_entry(entry: Mapping[str, Any], *, server: str) -> Candidate:
    """An entry from providers/biorxiv.py's own fetch_page() 'collection' list, for either server
    ("biorxiv" or "medrxiv") -- the real API's own response shape is identical between the two, only
    values differ. The DOI (confirmed live, present on virtually every real record -- unlike IACR eprints,
    which mostly have none) becomes both Candidate.doi (the universal field) AND this source's own
    external_ids key, the same value in both places: bioRxiv/medRxiv preprints have no other native id, so
    the external_ids entry exists purely so harvest_ingest_candidate's own find-by-source-key lookup and
    search_page's own per-server isolation filter have something to key on. `authors` is a single "; "-joined
    string in the real API ("Last, F.; Last2, F2."), confirmed live -- split and stripped. `date` is the
    record's own real publication date (confirmed live: unlike IACR's OAI datestamp, which is last-MODIFIED,
    this is the preprint's own posting date), so year comes directly from it, no id-prefix workaround needed.
    Never a PDF: this plan's own scope is getting these two sources searchable, not fetching their files."""
    doi = entry["doi"]
    authors = [name.strip() for name in (entry.get("authors") or "").split(";") if name.strip()]
    date = entry.get("date") or ""
    return Candidate(
        title=entry.get("title") or "Untitled",
        authors=authors[:MAX_AUTHORS],
        year=int(date[:4]) if date[:4].isdigit() else None,
        doi=doi,
        external_ids={server: doi},
        abstract=entry.get("abstract") or None,
        sources=(server,),
    )


def from_biorxiv(entry: Mapping[str, Any]) -> Candidate:
    """Single-arg wrapper around from_biorxiv_entry for the registry's own Mapper slot
    (Callable[[Any], Candidate | None], no room for a server argument)."""
    return from_biorxiv_entry(entry, server="biorxiv")


def from_medrxiv(entry: Mapping[str, Any]) -> Candidate:
    """Single-arg wrapper around from_biorxiv_entry for the registry's own Mapper slot."""
    return from_biorxiv_entry(entry, server="medrxiv")


def normal_title(title: str) -> str:
    return re.sub(r"\W+", " ", title).strip().casefold()


def surname(name: str) -> str:
    """ "Uszkoreit, Jakob" and "Jakob Uszkoreit" both give "uszkoreit"; accents are dropped ("Krönke" → "kronke")."""
    part = name.split(",")[0] if "," in name else next(reversed(name.split()), "")
    return "".join(c for c in unicodedata.normalize("NFKD", part) if not unicodedata.combining(c)).strip().casefold()


def _ids(candidate: Candidate) -> set[str]:
    # ponytail: hardcodes today's id kinds — can't derive this list from source_registry (it imports
    # *from* this module, so importing it back would be circular). Add one line here per future source;
    # restructure only if that becomes its own recurring chore across several sources at once.
    arxiv_id = candidate.external_ids.get("arxiv") or arxiv_from_doi(candidate.doi)
    keys = {
        "doi": None if arxiv_from_doi(candidate.doi) else candidate.doi,
        "arxiv": arxiv_id,
        "openalex": candidate.external_ids.get("openalex"),
        "s2": candidate.external_ids.get("semantic_scholar"),
        "core": candidate.external_ids.get("core"),
        "pubmed": candidate.external_ids.get("pubmed"),
        "pmc": candidate.external_ids.get("pmc"),
        "zenodo": candidate.external_ids.get("zenodo"),
        "hal": candidate.external_ids.get("hal"),
        "doaj": candidate.external_ids.get("doaj"),
        "openaire": candidate.external_ids.get("openaire"),
        "iacr_eprint": candidate.external_ids.get("iacr_eprint"),
        "biorxiv": candidate.external_ids.get("biorxiv"),
        "medrxiv": candidate.external_ids.get("medrxiv"),
        "dblp": candidate.external_ids.get("dblp"),
    }
    return {f"{kind}:{value.lower()}" for kind, value in keys.items() if value}


def _same_paper(a: Candidate, b: Candidate) -> bool:
    if _ids(a) & _ids(b):
        return True
    if normal_title(a.title) != normal_title(b.title):
        return False
    if a.authors and b.authors:
        return bool({surname(n) for n in a.authors} & {surname(n) for n in b.authors})
    return a.year is not None and a.year == b.year


def _combined(records: list[Candidate]) -> Candidate:
    """`records` most trusted first: each field from the first record that has it."""

    def first(attribute: str):
        return next((value for r in records if (value := getattr(r, attribute))), None)

    def first_id(source: str) -> str | None:
        return next((value for r in records if (value := r.external_ids.get(source))), None)

    arxiv_id = first_id("arxiv")
    external_ids = {
        k: v
        for k, v in {
            "dblp": first_id("dblp"),
            "arxiv": arxiv_id, "openalex": first_id("openalex"), "semantic_scholar": first_id("semantic_scholar"),
            "core": first_id("core"), "pubmed": first_id("pubmed"), "pmc": first_id("pmc"),
            "zenodo": first_id("zenodo"), "hal": first_id("hal"), "doaj": first_id("doaj"),
            "openaire": first_id("openaire"), "iacr_eprint": first_id("iacr_eprint"),
            "biorxiv": first_id("biorxiv"), "medrxiv": first_id("medrxiv"),
        }.items()
        if v
    }
    counts = [r.cited_by_count for r in records if r.cited_by_count is not None]
    return Candidate(
        title=records[0].title,
        authors=first("authors") or [],
        year=first("year"),
        venue=first("venue"),
        doi=first("doi"),
        external_ids=external_ids,
        cited_by_count=max(counts, default=None),
        abstract=first("abstract"),
        pdf_urls=ordered_pdf_urls(arxiv_id, *(url for r in records for url in r.pdf_urls)),
        sources=tuple(dict.fromkeys(source for r in records for source in r.sources)),
        source_type=first("source_type"),
        published_doi=first("published_doi"),
        published_venue=first("published_venue"),
    )


def merge(found: Mapping[str, list[Candidate]], limit: int) -> list[Candidate]:
    """One candidate per paper (D73). `found` maps each source to its results, most trusted source first. Papers more
    sources found come first, then the best position any source gave them."""
    # ponytail: a record matching two groups joins the first; merging the groups needs union-find, add it if a
    # search ever shows one paper twice.
    groups: list[list[tuple[int, Candidate]]] = []
    for results in found.values():
        for position, candidate in enumerate(results):
            group = next((g for g in groups if any(_same_paper(candidate, other) for _, other in g)), None)
            if group is None:
                groups.append([(position, candidate)])
            else:
                group.append((position, candidate))
    ranked = sorted(groups, key=lambda g: (-len({s for _, c in g for s in c.sources}), min(p for p, _ in g)))
    return [_combined([candidate for _, candidate in group]) for group in ranked[:limit]]
