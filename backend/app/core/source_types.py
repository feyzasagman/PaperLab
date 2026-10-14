"""What kind of source a paper is, and whether it was peer-reviewed (M39 F6, D249). Heuristics, shown as such.

`magazine` is never inferred: no reliable signal. The owner sets it by hand.
"""

from typing import Literal, get_args

SourceType = Literal["journal_article", "proceedings", "book", "chapter", "preprint", "magazine", "thesis", "other"]
PeerReviewed = Literal["true", "false", "unknown"]
SOURCE_TYPES: tuple[str, ...] = get_args(SourceType)
PEER_REVIEWED: tuple[str, ...] = get_args(PeerReviewed)

ARXIV_DOI_PREFIX = "10.48550/arxiv."

CROSSREF_TYPES = {
    "journal-article": "journal_article",
    "proceedings-article": "proceedings",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "book-chapter": "chapter",
    "book-section": "chapter",
    "book-part": "chapter",
    "posted-content": "preprint",
    "dissertation": "thesis",
}
OPENALEX_TYPES = {
    "article": "journal_article",
    "review": "journal_article",
    "preprint": "preprint",
    "conference-paper": "proceedings",
    "proceedings-article": "proceedings",
    "book": "book",
    "book-chapter": "chapter",
    "dissertation": "thesis",
}
_PEER = {"preprint": "false", "journal_article": "true", "proceedings": "true"}


def source_type_of(raw_type: str | None, doi: str | None, *, crossref: bool = False) -> str | None:
    """The source type for an OpenAlex (or, with crossref=True, Crossref) `type`. None when there is no type and
    nothing else to go on. An arXiv DOI means a preprint whatever the type says; a paper with a journal DOI never
    gets that rule, so the journal type wins."""
    if doi and doi.lower().startswith(ARXIV_DOI_PREFIX):
        return "preprint"
    if not raw_type:
        return None
    return (CROSSREF_TYPES if crossref else OPENALEX_TYPES).get(raw_type.lower(), "other")


def peer_reviewed_of(source_type: str | None) -> str:
    return _PEER.get(source_type or "", "unknown")


def evidence_access(abstract: str | None, status: str, has_chunks: bool) -> str:
    """full_text only when the paper is ready and has chunks; a paper still ingesting with an abstract is 'abstract'."""
    if status == "ready" and has_chunks:
        return "full_text"
    return "abstract" if abstract else "metadata"
