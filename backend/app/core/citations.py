"""Finding citations in prose and matching them to library papers (M39 F4). Sentences, DOIs and author-year forms
only; a citation never guesses: the caller sees how many papers matched."""

import re
import unicodedata
from dataclasses import dataclass, replace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enrichment import normalize_doi
from app.models import Paper, workspace_papers

ABBREVIATIONS = {
    "al.",
    "e.g.",
    "i.e.",
    "fig.",
    "figs.",
    "eq.",
    "eqs.",
    "dr.",
    "vs.",
    "cf.",
    "no.",
    "sec.",
    "tab.",
    "ref.",
    "refs.",
    "prof.",
    "mr.",
    "ms.",
    "approx.",
    "et.",
    "etc.",
    "ca.",
    "vol.",
    "pp.",
    "p.",
}
SENTENCE_STARTERS = {"In", "On", "Since", "After", "By", "Until", "During", "From", "Before", "For", "As", "At"}
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_DOI = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s]+)", re.I)
_PAREN = re.compile(r"\(([^()]*?(?:19|20)\d{2}[a-z]?[^()]*)\)")
_PART = re.compile(r"^(?P<names>[^\d]*?),?\s*(?P<year>(?:19|20)\d{2})[a-z]?\b")
# Words that open a sentence and are followed by a comma, so a name list can't start with them ("However, Smith and
# Lee (2020)"). ponytail: a word list; check candidate surnames against the library if a real surname collides.
LEAD_WORDS = frozenset(SENTENCE_STARTERS) | {
    "However",
    "Thus",
    "Hence",
    "Moreover",
    "Also",
    "Similarly",
    "Likewise",
    "Here",
    "Then",
    "Still",
    "Yet",
    "Indeed",
    "Finally",
    "First",
    "Second",
    "Third",
    "Further",
    "Furthermore",
    "Instead",
    "Conversely",
    "Notably",
    "Overall",
    "Again",
    "Therefore",
    "Meanwhile",
    "Recently",
    "Later",
    "Earlier",
    "Importantly",
    "Specifically",
    "Consequently",
    "Nevertheless",
    "Nonetheless",
    "Additionally",
    "Alternatively",
    "Accordingly",
}
_PARTICLE = r"(?:(?:van|von|der|den|de|del|della|da|di|du|dos|la|le|ter|ten)\s+)*"
_NAME = _PARTICLE + r"[A-Z][\w'’\-‐]+"
# "Smith (2020)", "Smith et al. (2020, p. 4)", "Smith & Lee (2020)", "Smith, Lee, and van Dijk (2020)"
_NARRATIVE = re.compile(
    rf"(?P<names>{_NAME}(?:\s+et al\.?|(?:,\s+{_NAME})*,?\s+(?:&|and)\s+{_NAME})?)"
    r"\s+\((?P<year>(?:19|20)\d{2})[a-z]?(?:,\s*(?:pp?\.|ch\.|sec\.)\s*[\w–\-]+)?\)"
)
_LEAD = re.compile(r"^(?:(?:see|also|e\.g\.,?|cf\.)\s+)+", re.I)
# "(X, 2012, as cited in Y, 2025)", "(quoted in Y, 2025)": the source the author read is Y (APA secondary citations).
_SECONDARY = re.compile(
    r"^(?P<orig>.*?)[,;]?\s*(?:as\s+)?(?:quoted|cited|summari[sz]ed|reported|discussed|described)\s+(?:in|by)\s+(?P<src>.+)$",
    re.I,
)
_INITIALS = re.compile(r"[a-z]{1,2}\.?|(?:[a-z]\.)+")  # "j", "jk", "j.k." (folded)
_INITIAL = re.compile(r"^\(?[A-Z]\.$")  # "J." before a surname: not a sentence end
_FIRST_AUTHOR = re.compile(r",|\s&\s|\sand\s|\set al", re.I)


@dataclass(frozen=True)
class Citation:
    kind: str  # "doi" | "author_year"
    text: str
    span: tuple[int, int]  # what to cut from the sentence (a parenthesis holding several citations is one span)
    doi: str | None = None
    surname: str | None = None
    year: int | None = None
    narrative: bool = False  # "Smith (2020) found": the names are part of the sentence, so a claim keeps them
    via: str | None = None  # the original work when this cites the source it was read in ("as cited in")
    superseded: bool = False  # an original the sentence says was read elsewhere: cut from the claim, never checked


@dataclass(frozen=True)
class Match:
    paper_ids: list
    titles: list[str]
    library_year: int | None = None  # the paper's year when it matched only as the neighbouring one (M2)


def split_sentences(text: str) -> list[str]:
    """Splits on . ! ? followed by a capital, digit or opening bracket, except after "et al.", "e.g.", "Fig." etc."""
    out: list[str] = []
    for piece in _BOUNDARY.split(text.strip()):
        last = out[-1].split()[-1] if out else ""
        if out and (last.lstrip("([").lower() in ABBREVIATIONS or _INITIAL.match(last)):
            out[-1] = f"{out[-1]} {piece}"
        elif piece.strip():
            out.append(piece.strip())
    return out


def find(sentence: str) -> list[Citation]:
    """DOIs, then author-year forms, in sentence order."""
    found: list[Citation] = []
    standalone: list[Citation] = []  # a "(quoted in Y, 2025)" with no original of its own
    for m in _DOI.finditer(sentence):
        doi = normalize_doi(m.group(1))
        if doi is None:
            continue
        end = m.start(1) + len(doi)  # normalising only trims and lowercases, so lengths line up
        found.append(Citation("doi", sentence[m.start() : end], (m.start(), end), doi=doi))
    for group in _PAREN.finditer(sentence):
        for part in group.group(1).split(";"):
            text = _LEAD.sub("", part.strip())
            secondary = _SECONDARY.match(text)
            parsed = secondary and _PART.match(secondary["src"])
            if not parsed:  # "discussed in detail below": no cited source with a year, so an ordinary citation
                secondary, parsed = None, _PART.match(text)
            if parsed and parsed["names"].strip()[:1].isupper():
                surname = _FIRST_AUTHOR.split(parsed["names"].strip())[0].strip()
                via = secondary["orig"].strip(" ,;") or None if secondary else None
                citation = Citation(
                    "author_year", part.strip(), group.span(), surname=surname, year=int(parsed["year"]), via=via
                )
                found.append(citation)
                if secondary and via is None:
                    standalone.append(citation)
    for m in _NARRATIVE.finditer(sentence):
        names, start = m["names"], m.start()
        while len(head := names.split(", ", 1)) == 2 and head[0] in LEAD_WORDS:
            start, names = start + len(head[0]) + 2, head[1]
        if names in LEAD_WORDS:
            continue
        surname = _FIRST_AUTHOR.split(names)[0].strip()
        found.append(
            Citation(
                "author_year",
                sentence[start : m.end()],
                (start, m.end()),
                surname=surname,
                year=int(m["year"]),
                narrative=True,
            )
        )
    if standalone:  # the narrative citations are the originals the author read through Y
        originals = [c.text for c in found if c.narrative]
        via = "; ".join(originals) or None
        found = [replace(c, superseded=True) if c.narrative else c for c in found]
        found = [replace(c, via=via) if c in standalone else c for c in found]
    return sorted(found, key=lambda c: c.span)


# Every dash a library or a note may use for a hyphenated name (Gómez‐Rodríguez with U+2010) compares as "-" (M3).
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−﹘﹣－", "-"))


def fold(text: str) -> str:
    """Case-, accent- and dash-insensitive form (Müller == muller, Gómez‐Rodríguez == Gomez-Rodriguez)."""
    text = unicodedata.normalize("NFKD", text.translate(_DASHES).casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def _first_author_is(authors: list[str], surname: str) -> bool:
    # the whole name must end with the surname, so "van der Berg" works and a given name never matches
    if not authors:
        return False
    name, wanted = fold(authors[0]).strip(), fold(surname).strip()
    if "," in name:  # "Smith, J." (bioRxiv, OpenAIRE): the surname comes first
        name = name.split(",")[0].strip()
    tokens = name.split()
    if len(tokens) > 1 and tokens[0] == wanted and all(_INITIALS.fullmatch(t) for t in tokens[1:]):
        return True  # "Smith J", "Smith J.K."
    return name == wanted or name.endswith(f" {wanted}")


async def match(session: AsyncSession, citation: Citation, workspace_id=None, only_doi: str | None = None) -> Match:
    """Library papers a citation points at: by DOI, or by first-author surname and year (inside a workspace if
    given). Zero or several matches are returned as they are, never resolved. `only_doi` (the reference list's DOI for
    this citation) drops author–year candidates whose own DOI is another one."""
    query = select(Paper.id, Paper.title, Paper.authors, Paper.year, Paper.doi)
    if workspace_id is not None:
        query = query.join(workspace_papers, workspace_papers.c.paper_id == Paper.id).where(
            workspace_papers.c.workspace_id == workspace_id
        )
    if citation.kind == "doi":
        query = query.where(func.lower(Paper.doi) == citation.doi.lower())
    else:
        query = query.where(Paper.year.between(citation.year - 1, citation.year + 1))
    rows = (await session.execute(query.order_by(Paper.title))).all()
    if citation.kind == "author_year":
        rows = [r for r in rows if _first_author_is(r.authors, citation.surname)]
        if only_doi:
            rows = [r for r in rows if not r.doi or normalize_doi(r.doi) == only_doi]
        exact = [r for r in rows if r.year == citation.year]
        # A preprint or online-first year often differs from the issue year by one (D279): the cited year wins, and a
        # lone neighbour matches with a warning; two neighbours are left ambiguous.
        if exact or len(rows) != 1:
            rows = exact or rows
        else:
            return Match([rows[0].id], [rows[0].title], library_year=rows[0].year)
    return Match([r.id for r in rows], [r.title for r in rows])
