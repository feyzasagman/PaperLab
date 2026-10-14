"""Markdown notes to the blocks claims are built in (M46, D276): paragraphs, list items and table rows, with headings,
list markers, emphasis, links, code and front matter gone, and the reference list split off. Wikilinks ([[note]]) stay:
claims.py judges cross-references by them, then turns them into words."""

import re
from dataclasses import dataclass

from app.core import citations
from app.core.enrichment import normalize_doi

# A line of its own: "## References", "**References**", "References:", also in French, Spanish and German.
_REFERENCES = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*"
    r"(?:references|reference list|bibliography|works cited|sources|r\u00e9f\u00e9rences|bibliograf\u00eda"
    r"|literaturverzeichnis|literatur)"
    r"\s*:?\s*(?:\*\*|__)?\s*:?\s*#*\s*$",
    re.I | re.M,
)
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FIRST_NUMBER = re.compile(r"^\s*1[.)]\s+")  # CommonMark: only "1." may interrupt a paragraph that isn't a list item
_LEAD = re.compile(r"^(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_QUOTE = re.compile(r"^\s*>\s?")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CELL = re.compile(r"(?<!\\)\|")
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EM = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\*)|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_LINK = re.compile(r"(?<!!)\[([^\]\[]+)\]\([^)]*\)")
_CODE = re.compile(r"`([^`]*)`")


@dataclass(frozen=True)
class Block:
    text: str  # plain prose; a table row's non-empty cells joined by spaces
    cells: tuple[str, ...] = ()  # a table row's cells, plain; empty for prose
    lead: str = ""  # the text of a bold span the block opens with ("**Smith (2020).** …"): the source's own label


def split_references(text: str) -> tuple[str, str]:
    """(body, reference list): everything from a References/Bibliography heading on is the list."""
    text = _FRONT_MATTER.sub("", text.replace("\r\n", "\n"))
    heading = _REFERENCES.search(text)
    return (text[: heading.start()], text[heading.end() :]) if heading else (text, "")


def inline(text: str) -> str:
    """Inline markdown to plain text, one space between words. Wikilinks are kept."""
    text = _CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _STRONG.sub(r"\2", text)
    text = _EM.sub(lambda m: m.group(1) or m.group(2), text)
    return " ".join(text.split())


def _row(line: str) -> Block:
    cells = tuple(inline(cell.replace("\\|", "|")) for cell in _CELL.split(line.strip().strip("|")))
    return Block(" ".join(cell for cell in cells if cell), cells)


def blocks(body: str) -> list[Block]:
    """The note's paragraphs, list items and table rows, in order. A table's header row and rule are skipped;
    headings, blank lines and fences end a paragraph; fenced code is dropped."""
    out: list[Block] = []
    paragraph: list[str] = []
    starts_as_item = False
    lines = body.split("\n")
    fenced = False

    def flush() -> None:
        raw = " ".join(paragraph)
        if text := inline(raw):
            lead = _LEAD.match(raw)
            out.append(Block(text, lead=inline(lead[2]) if lead else ""))
        paragraph.clear()

    for i, raw in enumerate(lines):
        if _FENCE.match(raw):
            flush()
            fenced = not fenced
            continue
        line = _QUOTE.sub("", raw)
        if fenced:
            continue
        if not line.strip() or _HEADING.match(line):
            flush()
        elif line.lstrip().startswith("|"):
            flush()
            header = i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1])
            if not _TABLE_RULE.match(line) and not header:
                out.append(_row(line))
        else:
            if _ITEM.match(line) and (not paragraph or starts_as_item or _FIRST_NUMBER.match(line)):
                flush()
                starts_as_item = True
                line = _ITEM.sub("", line)
            elif not paragraph:
                starts_as_item = False
            paragraph.append(line.strip())
    flush()
    return out


# "- Surname, I., … (2024). Title…": the first author's surname and the year of a reference-list entry.
_ENTRY = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*(?P<surname>[^,(]+?),.*?\((?P<year>(?:19|20)\d{2})[a-z]?[,)]")


_AUTHOR = re.compile(r"(?:^|[,&]\s*)(?:&\s*)?([^,&()]+?),\s+(?:[A-Z][a-z]?\.[\s-]*)+")


@dataclass(frozen=True)
class RefEntry:
    doi: str | None
    surnames: tuple[str, ...]  # folded, first author first


def reference_entries(references: str) -> dict[tuple[str, int], list[RefEntry]]:
    """(folded first-author surname, year) -> the reference-list entries with that key, each with its DOI (or None)
    and every author's folded surname (to tell "Lu, Wang et al." from "Lu, Sclar et al.", pilot 2 N4)."""
    entries: dict[tuple[str, int], list[RefEntry]] = {}
    for line in references.split("\n"):
        if entry := _ENTRY.match(line):
            doi = citations._DOI.search(line)
            head = line.split("(", 1)[0].lstrip("-*+0123456789.) ")
            first = citations.fold(entry["surname"].strip())
            surnames = tuple(citations.fold(s.strip()) for s in _AUTHOR.findall(head)) or (first,)
            entries.setdefault((first, int(entry["year"])), []).append(
                RefEntry(normalize_doi(doi.group(1)) if doi else None, surnames)
            )
    return entries


def reference_dois(references: str) -> dict[tuple[str, int], str]:
    """(folded first-author surname, year) -> DOI for each entry that gives one (D279). A key two entries share is left
    out even when only one of them has a DOI: the citation can't say which paper it means (the benchmarks note cites
    two Lu (2025) papers, one without a DOI)."""
    return {key: es[0].doi for key, es in reference_entries(references).items() if len(es) == 1 and es[0].doi}


def shared_keys(references: str) -> set[tuple[str, int]]:
    """The (surname, year) keys two or more reference entries share: a citation with such a key is ambiguous."""
    return {key for key, es in reference_entries(references).items() if len(es) > 1}
