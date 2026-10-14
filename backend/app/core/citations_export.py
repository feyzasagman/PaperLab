"""Citation export (M39 F7, D253-D255): one CSL-JSON item per paper is the hub; every format is rendered from it."""

import html
import json
import re
import unicodedata
import uuid
import warnings
from pathlib import Path

import bibtexparser
import rispy
from citeproc import Citation, CitationItem, CitationStylesBibliography, CitationStylesStyle, formatter
from citeproc.source.json import CiteProcJSON
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import workspaces
from app.core.errors import InvalidInput, NotFound
from app.models import Paper

CSL_TYPES = {
    "journal_article": "article-journal",
    "proceedings": "paper-conference",
    "book": "book",
    "chapter": "chapter",
    "preprint": "article",
    "magazine": "article-magazine",
    "thesis": "thesis",
    "other": "document",
}
ARXIV_DOI_PREFIX = "10.48550/arxiv."
ORG_WORDS = {
    "organization", "organisation", "university", "institute", "association", "group", "consortium", "committee",
    "society", "agency", "ministry", "team", "collaboration", "council", "center", "centre", "foundation",
    "laboratory", "department", "commission", "network", "company", "inc", "ltd",
}  # fmt: skip
PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "la", "le", "du", "bin", "al", "ter", "ten"}
SUFFIX = re.compile(r"[,\s]+(Jr\.?|Sr\.?|II|III|IV)$", re.IGNORECASE)
INITIALS = re.compile(r"[A-Z]{1,2}|(?:[A-Z]\.){1,3}")
TRANSLITERATE = str.maketrans({"ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "þ": "th"})
ARTICLES = ("a ", "an ", "the ")
STOP_WORDS = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "with", "from", "by", "at", "as", "is", "are"}
ACCESS_NOTE = {
    "full_text": "full text in PaperLab",
    "abstract": "abstract only in PaperLab",
    "metadata": "metadata only in PaperLab",
}
PEER_NOTE = {"true": "peer-reviewed", "false": "not peer-reviewed"}


def _person(name: str) -> dict:
    name = " ".join(name.split())
    suffix = None
    if (m := SUFFIX.search(name)) and " " in name[: m.start()].strip():
        name, suffix = name[: m.start()].strip(), m.group(1)
    words = re.findall(r"\w+", name.lower())
    # ponytail: an organisation is a single token or one with an organisation word; the byline has no other marker.
    if " " not in name and "," not in name or ORG_WORDS & set(words):
        return {"literal": name}
    if "," in name:
        family, _, given = name.partition(",")
        person = {"family": family.strip(), "given": given.strip()}
    else:
        tokens = name.split(" ")
        cut = len(tokens) - 1
        if INITIALS.fullmatch(tokens[-1]) and len(tokens) > 1:
            # "Smith J.A.": the trailing initials are the given name
            person = {"family": " ".join(tokens[:cut]), "given": tokens[-1]}
        else:
            while cut > 1 and tokens[cut - 1].lower() in PARTICLES:
                cut -= 1
            person = {"family": " ".join(tokens[cut:]), "given": " ".join(tokens[:cut])}
    return {**person, "suffix": suffix} if suffix else person


def _note(paper) -> str | None:
    label = (paper.source_type or "").replace("_", " ")
    preprint = paper.source_type == "preprint"  # the genre is already "Preprint"; don't say it twice
    parts = [
        f"Source: {label}" if label and not preprint else None,
        PEER_NOTE.get(paper.peer_reviewed),
        ACCESS_NOTE.get(paper.evidence_access),
    ]
    note = " · ".join(p for p in parts if p)
    return note[:1].upper() + note[1:] if preprint and note else note or None


def csl_item(paper) -> dict:
    """The CSL-JSON item for a paper-like object (no DB). Optional editors/volume/issue/pages are read when present."""
    item: dict = {
        "id": str(paper.id),
        "type": CSL_TYPES.get(paper.source_type or "journal_article", "document"),
        "title": paper.title,
    }
    if paper.authors:
        item["author"] = [_person(name) for name in paper.authors]
    if editors := getattr(paper, "editors", None):
        item["editor"] = [_person(name) for name in editors]
    if paper.year:
        item["issued"] = {"date-parts": [[paper.year]]}
    if paper.source_type == "preprint":
        item["genre"] = "Preprint"
        arxiv = (paper.doi or "").lower().startswith(ARXIV_DOI_PREFIX)
        if publisher := paper.venue or ("arXiv" if arxiv else None):
            item["publisher"] = publisher
    elif paper.venue:
        item["container-title"] = paper.venue
    for key, attr in (("volume", "volume"), ("issue", "issue"), ("page", "pages")):
        if value := getattr(paper, attr, None):
            item[key] = str(value)
    if paper.doi:
        item["DOI"] = paper.doi
    elif paper.oa_url:
        item["URL"] = paper.oa_url
    if note := _note(paper):
        item["note"] = note
    return item


def _ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().translate(TRANSLITERATE)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def _base_key(item: dict) -> str:
    first = (item.get("author") or [{}])[0]
    name = first.get("family") or (first.get("literal") or "").split(" ")[0]
    year = item.get("issued", {}).get("date-parts", [[None]])[0][0]
    words = [_ascii(w) for w in item["title"].split()]
    word = next((w for w in words if w and w not in STOP_WORDS), "") or next((w for w in words if w), "")
    # ponytail: a name or title with no Latin letters (CJK, Cyrillic) falls back to "anon" / "untitled".
    return f"{_ascii(name) or 'anon'}{year or 'nd'}{word or 'untitled'}"


def bibtex_keys(items: list[dict]) -> dict[str, str]:
    """item id -> key `firstauthorYEARfirstword`. Collisions in one export get a, b, ... by title, then id, so the
    same library gives the same keys every time."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(_base_key(item), []).append(item)
    keys: dict[str, str] = {}
    for base, group in groups.items():
        group.sort(key=lambda i: (i["title"].lower(), i["id"]))
        for n, item in enumerate(group):
            keys[item["id"]] = base if len(group) == 1 else base + _suffix(n)
    return keys


def _suffix(n: int) -> str:
    return (_suffix(n // 26 - 1) if n >= 26 else "") + chr(97 + n % 26)


# ---- renderers ----

APA_STYLE = Path(__file__).resolve().parents[1] / "styles" / "apa.csl"
FORMATS = {
    "bibtex": ("application/x-bibtex", "bib"),
    "ris": ("application/x-research-info-systems", "ris"),
    "csljson": ("application/vnd.citationstyles.csl+json", "json"),
    "apa": ("text/plain", "txt"),
    "apa-html": ("text/html", "html"),
}
BIBTEX_TYPES = {
    "article-journal": "article",
    "article-magazine": "article",
    "paper-conference": "inproceedings",
    "book": "book",
    "chapter": "incollection",
    "thesis": "phdthesis",
}
RIS_TYPES = {
    "article-journal": "JOUR",
    "article-magazine": "MGZN",
    "paper-conference": "CONF",
    "book": "BOOK",
    "chapter": "CHAP",
    "thesis": "THES",
}
BIBTEX_SPECIALS = str.maketrans(
    {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
     "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
)  # fmt: skip


def render(items: list[dict], fmt: str) -> str:
    """The items as one of FORMATS, in the order given (APA sorts itself). ValueError("unknown_format") otherwise."""
    if fmt not in FORMATS:
        raise ValueError("unknown_format")
    if fmt == "csljson":
        return json.dumps(items, ensure_ascii=False, indent=2)
    if fmt == "bibtex":
        return _bibtex(items)
    if fmt == "ris":
        return _ris(items)
    return _apa(items, html=fmt == "apa-html")


def _tex(text: str | None) -> str | None:
    return text.translate(BIBTEX_SPECIALS) if text else text


def _bibtex_names(people: list[dict]) -> str:
    return " and ".join(
        "{" + _tex(p["literal"]) + "}" if "literal" in p else _tex(f"{p['family']}, {p['given']}".rstrip(", "))
        for p in people
    )


def _bibtex_fields(item: dict) -> dict[str, str]:
    kind = item["type"]
    container = item.get("container-title")
    fields = {
        "author": _bibtex_names(item.get("author", [])),
        "editor": _bibtex_names(item.get("editor", [])),
        "title": item["title"],
        "journal": container if kind in ("article-journal", "article-magazine") else None,
        "booktitle": container if kind in ("paper-conference", "chapter") else None,
        "publisher": item.get("publisher"),
        "year": str(item.get("issued", {}).get("date-parts", [[""]])[0][0]),
        "volume": item.get("volume"),
        "number": item.get("issue"),
        "pages": re.sub(r"^(\d+)\s*[-–]+\s*(\d+)$", r"\1--\2", item.get("page") or "") or None,
        "note": ". ".join(p for p in (item.get("genre"), item.get("note")) if p),
    }
    escaped = {k: v if k in ("author", "editor") else _tex(v) for k, v in fields.items()}
    # DOIs and URLs are verbatim in BibTeX; escaping their underscores or percents would break the link.
    return {**escaped, "doi": item.get("DOI"), "url": item.get("URL")}


def _bibtex(items: list[dict]) -> str:
    keys = bibtex_keys(items)
    library = bibtexparser.Library()
    for item in items:
        fields = [bibtexparser.model.Field(k, v) for k, v in _bibtex_fields(item).items() if v]
        entry_type = BIBTEX_TYPES.get(item["type"], "misc")
        library.add(bibtexparser.model.Entry(entry_type, keys[item["id"]], fields))
    return bibtexparser.write_string(library)


def _ris_names(people: list[dict]) -> list[str]:
    return [p["literal"] if "literal" in p else f"{p['family']}, {p['given']}".rstrip(", ") for p in people]


def _ris_entry(item: dict) -> dict:
    m = re.fullmatch(r"(\d+)\s*[-–]+\s*(\d+)", item.get("page") or "")
    first, last = m.groups() if m else (item.get("page"), None)
    entry = {
        "type_of_reference": RIS_TYPES.get(item["type"], "GEN"),
        "title": item["title"],
        "authors": _ris_names(item.get("author", [])),
        "secondary_authors": _ris_names(item.get("editor", [])),
        "secondary_title": item.get("container-title"),
        "publisher": item.get("publisher"),
        "year": str(item.get("issued", {}).get("date-parts", [[""]])[0][0]),
        "volume": item.get("volume"),
        "number": item.get("issue"),
        "start_page": first or None,
        "end_page": last,
        "doi": item.get("DOI"),
        "urls": [item["URL"]] if item.get("URL") else None,
        "notes": [". ".join(p for p in (item.get("genre"), item.get("note")) if p)],
    }
    return {k: v for k, v in entry.items() if v}


def _ris(items: list[dict]) -> str:
    return rispy.dumps([_ris_entry(item) for item in items])


def _initials(given: str) -> str:
    return "".join(w[0] for w in re.findall(r"\w+", given.lower()))


def _title_key(title: str) -> str:
    lowered = title.lower().strip()
    return next((lowered[len(a) :] for a in ARTICLES if lowered.startswith(a)), lowered)


def _apa_names(item: dict) -> tuple:
    people = item.get("author") or [{"literal": _title_key(item["title"])}]
    fold = lambda t: unicodedata.normalize("NFKD", t).lower()  # noqa: E731
    return tuple((fold(p.get("family") or p.get("literal") or ""), _initials(p.get("given", ""))) for p in people)


def _apa_order(item: dict) -> tuple:
    # citeproc-py's own bibliography.sort() leaves the order unchanged with this style, so order by authors (a shorter
    # list first), year, title.
    year = item.get("issued", {}).get("date-parts", [[0]])[0][0]
    return (_apa_names(item), year, _title_key(item["title"]), item["id"])


def _escaped(value):
    if isinstance(value, str):
        return html.escape(value, quote=False)
    if isinstance(value, list):
        return [_escaped(v) for v in value]
    if isinstance(value, dict):
        return {k: v if k in ("id", "type") else _escaped(v) for k, v in value.items()}
    return value


def _with_year_suffixes(ordered: list[dict]) -> list[dict]:
    """citeproc-py doesn't compute year suffixes: same authors and year get a, b, ... in the final order."""
    groups: dict[tuple, list[dict]] = {}
    for item in ordered:
        if item.get("issued"):
            groups.setdefault((_apa_names(item), _apa_order(item)[1]), []).append(item)
    suffixes = {i["id"]: _suffix(n) for g in groups.values() if len(g) > 1 for n, i in enumerate(g)}
    return [{**i, "year-suffix": suffixes[i["id"]]} if i["id"] in suffixes else i for i in ordered]


def _apa(items: list[dict], *, html: bool) -> str:
    ordered = _with_year_suffixes(sorted(items, key=_apa_order))
    if html:
        ordered = [_escaped(i) for i in ordered]  # citeproc's html formatter leaves field values raw
    # ponytail: the style is parsed on every call (~0.3 s); cache it if exports get hot.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # citeproc-py warns about CSL fields it doesn't know
        style = CitationStylesStyle(str(APA_STYLE), validate=False)
        bibliography = CitationStylesBibliography(
            style, CiteProcJSON(ordered), formatter.html if html else formatter.plain
        )
        for item in ordered:
            bibliography.register(Citation([CitationItem(item["id"])]))
        return "\n".join(str(entry) for entry in bibliography.bibliography()) + "\n"


MAX_SELECTION = 1000


async def export_papers(
    session: AsyncSession, *, workspace_id: uuid.UUID | None = None, paper_ids: list[uuid.UUID] | None = None, fmt: str
) -> tuple[str, int]:
    """The workspace's papers (its own order) or the selected ones (the order given, repeats dropped) rendered as `fmt`.
    Raises InvalidInput unknown_format / no_papers / too_many_papers, NotFound for an unknown workspace or paper."""
    if fmt not in FORMATS:
        raise InvalidInput("unknown_format")
    if workspace_id is not None:
        papers = await workspaces.papers(session, workspace_id)
    else:
        if len(paper_ids or []) > MAX_SELECTION:
            raise InvalidInput("too_many_papers")
        ids = list(dict.fromkeys(paper_ids or []))
        found = {p.id: p for p in await session.scalars(select(Paper).where(Paper.id.in_(ids)))}
        if missing := [i for i in ids if i not in found]:
            raise NotFound(f"paper {missing[0]} not found")
        papers = [found[i] for i in ids]
    if not papers:
        raise InvalidInput("no_papers")
    return render([csl_item(p) for p in papers], fmt), len(papers)


def filename_slug(name: str) -> str:
    """A download-safe name: lowercase ASCII words joined by hyphens."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-") or "citations"
