"""Citation export (M39 F7, D253-D255): one CSL-JSON item per paper is the hub; every format is rendered from it."""

import re
import unicodedata

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
STOP_WORDS = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "with", "from", "by", "at", "as", "is", "are"}
ACCESS_NOTE = {
    "full_text": "full text in PaperLab",
    "abstract": "abstract only in PaperLab",
    "metadata": "metadata only in PaperLab",
}
PEER_NOTE = {"true": "peer-reviewed", "false": "not peer-reviewed"}


def _person(name: str) -> dict:
    name = " ".join(name.split())
    words = re.findall(r"\w+", name.lower())
    # ponytail: an organisation is a single token or one with an organisation word; the byline has no other marker.
    if " " not in name and "," not in name or ORG_WORDS & set(words):
        return {"literal": name}
    if "," in name:
        family, _, given = name.partition(",")
        return {"family": family.strip(), "given": given.strip()}
    tokens = name.split(" ")
    cut = len(tokens) - 1
    while cut > 1 and tokens[cut - 1].lower() in PARTICLES:
        cut -= 1
    return {"family": " ".join(tokens[cut:]), "given": " ".join(tokens[:cut])}


def _note(paper) -> str | None:
    label = (paper.source_type or "").replace("_", " ")
    parts = [
        f"Source: {label}" if label else None,
        PEER_NOTE.get(paper.peer_reviewed),
        ACCESS_NOTE.get(paper.evidence_access),
    ]
    return " · ".join(p for p in parts if p) or None


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
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def _base_key(item: dict) -> str:
    first = (item.get("author") or [{}])[0]
    name = first.get("family") or (first.get("literal") or "").split(" ")[0]
    year = item.get("issued", {}).get("date-parts", [[None]])[0][0]
    words = [_ascii(w) for w in item["title"].split()]
    word = next((w for w in words if w and w not in STOP_WORDS), "") or next((w for w in words if w), "")
    return f"{_ascii(name)}{year or 'nd'}{word}"


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
