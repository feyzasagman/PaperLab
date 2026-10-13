"""Finding citations in prose and matching them to library papers (M39 F4). Sentences, DOIs and author-year forms
only; a citation never guesses: the caller sees how many papers matched."""

import re
import unicodedata
from dataclasses import dataclass

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
_NARRATIVE = re.compile(
    r"(?P<names>[A-Z][\w'’\-]+(?:\s+et al\.?|\s+(?:&|and)\s+[A-Z][\w'’\-]+)?)\s+\((?P<year>(?:19|20)\d{2})[a-z]?\)"
)
_LEAD = re.compile(r"^(?:(?:see|also|e\.g\.,?|cf\.)\s+)+", re.I)
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


@dataclass(frozen=True)
class Match:
    paper_ids: list
    titles: list[str]


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
    for m in _DOI.finditer(sentence):
        doi = normalize_doi(m.group(1))
        if doi is None:
            continue
        end = m.start(1) + len(doi)  # normalising only trims and lowercases, so lengths line up
        found.append(Citation("doi", sentence[m.start() : end], (m.start(), end), doi=doi))
    for group in _PAREN.finditer(sentence):
        for part in group.group(1).split(";"):
            parsed = _PART.match(_LEAD.sub("", part.strip()))
            if parsed and parsed["names"].strip()[:1].isupper():
                surname = _FIRST_AUTHOR.split(parsed["names"].strip())[0].strip()
                found.append(
                    Citation("author_year", part.strip(), group.span(), surname=surname, year=int(parsed["year"]))
                )
    for m in _NARRATIVE.finditer(sentence):
        if m["names"] in SENTENCE_STARTERS:
            continue
        surname = _FIRST_AUTHOR.split(m["names"])[0].strip()
        found.append(Citation("author_year", m.group(0), m.span(), surname=surname, year=int(m["year"])))
    return sorted(found, key=lambda c: c.span)


def fold(text: str) -> str:
    """Case- and accent-insensitive form (Müller == muller)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def _first_author_is(authors: list[str], surname: str) -> bool:
    # the whole name must end with the surname, so "van der Berg" works and a given name never matches
    if not authors:
        return False
    name, wanted = fold(authors[0]).strip(), fold(surname).strip()
    if "," in name:  # "Smith, J." (bioRxiv, OpenAIRE): the surname comes first
        name = name.split(",")[0].strip()
    tokens = name.split()
    if len(tokens) > 1 and tokens[0] == wanted and all(re.fullmatch(r"[a-z]{1,2}\.?|(?:[a-z]\.)+", t) for t in tokens[1:]):
        return True  # "Smith J", "Smith J.K."
    return name == wanted or name.endswith(f" {wanted}")


async def match(session: AsyncSession, citation: Citation, workspace_id=None) -> Match:
    """Library papers a citation points at: by DOI, or by first-author surname and year (inside a workspace if
    given). Zero or several matches are returned as they are, never resolved."""
    query = select(Paper.id, Paper.title, Paper.authors)
    if workspace_id is not None:
        query = query.join(workspace_papers, workspace_papers.c.paper_id == Paper.id).where(
            workspace_papers.c.workspace_id == workspace_id
        )
    if citation.kind == "doi":
        query = query.where(func.lower(Paper.doi) == citation.doi.lower())
    else:
        query = query.where(Paper.year == citation.year)
    rows = (await session.execute(query.order_by(Paper.title))).all()
    if citation.kind == "author_year":
        rows = [r for r in rows if _first_author_is(r.authors, citation.surname)]
    return Match([r.id for r in rows], [r.title for r in rows])
