"""Claim verification (M39 F4): is a claim supported by a library paper? One model call over the paper's best
passages, a verbatim quote that is checked against them, and an llm_outputs row for provenance."""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import citations, claim_text, llm_connections, llm_json, notes, prompts, quotes, retrieval
from app.core.claim_text import RefEntry
from app.core.enrichment import normalize_doi
from app.core.errors import DomainError, InvalidInput, NotFound
from app.models import Chunk, LLMOutput, Paper, PaperStatus
from app.providers.base import LLMError, LLMUnavailable
from app.providers.llm import build_llm

CLAIM_PROMPT_VERSION = 1
PASSAGES = 6
SYSTEM_PROMPT, PROMPT_TEMPLATE = prompts.load("claim_check", CLAIM_PROMPT_VERSION).split("\n<!-- prompt -->\n")
OUTPUT_KIND = "claim_check"
VERDICT_WORDS = {
    "supported": "Supported",
    "partially_supported": "Partly supported",
    "not_supported": "Not supported",
    "not_addressed": "Not addressed",
}
CHECKED = ("supported", "partially_supported")  # verdicts that must rest on a kept quote


class _Reply(BaseModel):
    verdict: Literal["supported", "partially_supported", "not_supported", "not_addressed"]
    passage: int | None = None
    quote: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ClaimCheck:
    claim: str
    paper_id: uuid.UUID
    paper_title: str
    verdict: str  # supported | partially_supported | not_supported | not_addressed | cannot_verify
    reason: str
    evidence_level: str  # full_text | abstract_only | metadata_only
    quote: str | None = None
    quote_page: int | None = None
    quote_section: str | None = None
    quote_chunk_id: uuid.UUID | None = None
    passages: list[dict] = field(default_factory=list)  # {chunk_id, page, section}; chunk_id None for an abstract
    confidence: str | None = None  # high | low
    model: str | None = None
    prompt_version: int = CLAIM_PROMPT_VERSION
    output_id: uuid.UUID | None = None
    note_id: uuid.UUID | None = None
    paper_status: str | None = None  # set when the paper is still being ingested (reason not_ready)


async def _paper(session: AsyncSession, paper_id: uuid.UUID | None, doi: str | None) -> Paper:
    if (paper_id is None) == (not doi):
        raise InvalidInput("claim_scope", detail="Give either a paper_id or a doi.")
    if paper_id is not None:
        paper = await session.get(Paper, paper_id)
    else:
        wanted = normalize_doi(doi) or doi.strip().lower()
        paper = await session.scalar(select(Paper).where(func.lower(Paper.doi) == wanted))
    if paper is None:
        raise NotFound("paper_not_in_library", detail=f"{doi or paper_id} is not in the library.", doi=doi)
    return paper


LEAD_CHARS = 300  # how much of a sentence's start, from the chunk before, the verifier is shown
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s+")


async def _leads(session, found) -> list[str]:
    """For each passage that starts mid-sentence (its first letter is lower-case), the start of that sentence: the
    text after the last sentence end of the chunk before it. Chunks never cross a page, so a sentence over a page break
    is split, and its first half can hold the negation ("none of" | "the LLMs … correlate positively", pilot 2 N1).
    Context only: quotes are still taken from the passage itself."""
    leads = []
    for chunk in found:
        if not chunk.text.lstrip()[:1].islower():
            leads.append("")
            continue
        ordinal = select(Chunk.ordinal).where(Chunk.id == chunk.id).scalar_subquery()
        before = (
            await session.scalar(
                select(Chunk.text).where(Chunk.paper_id == chunk.paper_id, Chunk.ordinal == ordinal - 1)
            )
            or ""
        )
        ends = list(_SENTENCE_END.finditer(before))
        tail = (before[ends[-1].end() :] if ends else before).strip()
        leads.append(tail[-LEAD_CHARS:] if tail and not tail.endswith((".", "!", "?")) else "")
    return leads


async def _passages(
    session, paper: Paper, claim: str, quoted_text: str | None
) -> tuple[str, list[dict], list[str], list[str]]:
    """(evidence level, passage descriptions, passage texts, sentence starts): the best chunks, else the abstract."""
    has_chunks = paper.status == PaperStatus.READY and await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.paper_id == paper.id)
    )
    if has_chunks:
        found = await retrieval.retrieve(
            session, f"{claim} {quoted_text}" if quoted_text else claim, paper_ids=[paper.id], k=PASSAGES
        )
        if found:
            return (
                "full_text",
                [{"chunk_id": c.id, "page": c.page, "section": c.section} for c in found],
                [c.text for c in found],
                await _leads(session, found),
            )
    if (paper.abstract or "").strip():
        return "abstract_only", [{"chunk_id": None, "page": None, "section": None}], [paper.abstract], [""]
    return "metadata_only", [], [], []


def _prompt(claim: str, quoted_text: str | None, texts: list[str], passages: list[dict], leads: list[str]) -> str:
    def line(i, p, t, lead):
        if p["page"] is None:
            return f"[{i}] (abstract) {t}"
        cont = f" (continues a sentence that began: \"…{lead}\")" if lead else ""
        return f"[{i}] (page {p['page']}, section {p['section']}){cont} {t}"

    numbered = "\n\n".join(
        line(i, p, t, lead) for i, (p, t, lead) in enumerate(zip(passages, texts, leads, strict=True), start=1)
    )
    quoted = f"\nThe researcher quotes this from the paper: {quoted_text}\n" if quoted_text else ""
    # str.format only parses the template, so braces in the claim or passages are safe.
    return PROMPT_TEMPLATE.format(claim=claim, passages=numbered, quoted_text=quoted)


def _pick_quote(reply: _Reply, texts: list[str]) -> tuple[str | None, int | None, bool]:
    """(kept quote, 0-based passage index, a quote was asked for but dropped). The named passage is tried first
    (a number outside the passages counts as none), then every passage; the quote's passage wins over the named one."""
    if not reply.quote.strip():
        return None, None, False
    found = quotes.locate(reply.quote, reply.passage, texts)
    return (found[0], found[1], False) if found else (None, None, True)


async def _write_output(session, paper, claim, content: dict, passages, model, connection_name) -> uuid.UUID:
    output = LLMOutput(
        paper_id=paper.id,
        kind=OUTPUT_KIND,
        question=claim,
        content=json.dumps(content),
        source_chunks=[p["chunk_id"] for p in passages if p["chunk_id"] is not None],
        model=model,
        connection_name=connection_name,
        prompt_version=CLAIM_PROMPT_VERSION,
    )
    session.add(output)
    await session.commit()
    return output.id


async def _llm(session: AsyncSession, llm):
    if llm is not None:
        return llm
    connection, model = await llm_connections.resolve(session, None)
    return build_llm(connection, model.name)


async def verify_claim(
    session: AsyncSession,
    claim: str,
    *,
    paper_id: uuid.UUID | None = None,
    doi: str | None = None,
    quoted_text: str | None = None,
    llm=None,
    save: bool = False,
) -> ClaimCheck:
    """Checks `claim` against one library paper (by id or by DOI, exactly one) and returns the verdict.

    A metadata-only paper (no chunks, no abstract) is `cannot_verify` / `no_text`: no model call and no llm_outputs
    row, since nothing was asked. Every other check writes an llm_outputs row and returns its id as `output_id`,
    including two invalid model replies (`cannot_verify` / `model_error`), whose content records the failure.
    `save` writes the verdict as an AI note on the quoted passage (unanchored when it can't be placed) and returns
    its id as `note_id`; a `cannot_verify` check is never saved.
    """
    if not claim.strip():
        raise InvalidInput("empty_claim", detail="The claim is empty.")
    paper = await _paper(session, paper_id, doi)
    level, passages, texts, leads = await _passages(session, paper, claim, quoted_text)
    base = {"claim": claim, "paper_id": paper.id, "paper_title": paper.title, "evidence_level": level}
    if level == "metadata_only":
        if paper.status not in (PaperStatus.READY, PaperStatus.FAILED):  # still ingesting: text may come
            return ClaimCheck(**base, verdict="cannot_verify", reason="not_ready", paper_status=str(paper.status))
        return ClaimCheck(**base, verdict="cannot_verify", reason="no_text")
    llm = await _llm(session, llm)
    model = getattr(llm, "model", "unknown")
    connection_name = getattr(llm, "connection_name", None)
    reply = await llm_json.ask_json(llm, SYSTEM_PROMPT, _prompt(claim, quoted_text, texts, passages, leads), _Reply)
    if reply is None:
        content = {"verdict": "cannot_verify", "reason": "model_error"}
        output_id = await _write_output(session, paper, claim, content, passages, model, connection_name)
        return ClaimCheck(
            **base, verdict="cannot_verify", reason="model_error", passages=passages, model=model, output_id=output_id
        )
    quote, index, dropped = _pick_quote(reply, texts)
    chosen = passages[index] if index is not None else {}
    low = dropped or (reply.verdict in CHECKED and quote is None)
    check = ClaimCheck(
        **base,
        verdict=reply.verdict,
        reason=reply.reason.strip(),
        quote=quote,
        quote_page=chosen.get("page"),
        quote_section=chosen.get("section"),
        quote_chunk_id=chosen.get("chunk_id"),
        passages=passages,
        confidence="low" if low else "high",
        model=model,
    )
    content = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in asdict(check).items() if k != "passages"}
    output_id = await _write_output(session, paper, claim, content, passages, model, connection_name)
    note_id = None
    if save:
        body = f"Claim: {claim}\nVerdict: {VERDICT_WORDS[check.verdict]}. {check.reason}"
        note = await notes.create_ai_note(session, paper.id, body, output_id, quote)
        note_id = note.id
    return ClaimCheck(**{**asdict(check), "output_id": output_id, "note_id": note_id})


MAX_CLAIMS = 30


@dataclass(frozen=True)
class ClaimItem:
    claim: str  # the sentence with its citation(s) removed
    citation: citations.Citation
    check: ClaimCheck | None = None
    error: str | None = None  # citation_not_found | paper_not_in_library | ambiguous_citation | …
    candidates: list[str] = field(default_factory=list)  # titles when ambiguous
    warning: str | None = None  # year_mismatch: matched a paper the library dates a year away
    library_year: int | None = None
    doi: str | None = None  # paper_not_in_library: the reference list's DOI for it


_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_SEE = re.compile(r"\(?\b[Ss]ee(?:\s+also)?\s+(?:the\s+)?\[\[[^\]]+\]\](?:\s*(?:,|and|or)\s*\[\[[^\]]+\]\])*\)?\.?")
# "… are in [[note]]." / "… is covered in [[note]]" at the end, or a "[[note]]: …" list item: it says where something is
# covered, not what a paper found (D278). Anything else with a wikilink is a claim.
_CROSS_REFERENCE = re.compile(
    r"\b(?:is|are)\s+(?:(?:covered|discussed|treated|described|summari[sz]ed|reviewed|listed)\s+)?in\s+"
    r"\[\[[^\]]+\]\]\s*\.?$|^\[\[[^\]]+\]\]\s*:"
)


@dataclass(frozen=True)
class Cited:
    claim: str  # what the paper is checked against, plain text
    citation: citations.Citation
    error: str | None = None  # not_a_claim


def _kept_name(c: citations.Citation) -> str:
    return c.text[: c.text.rindex("(")].rstrip()


def _without(sentence: str, found: list[citations.Citation], keep_names: bool = True) -> str:
    """The sentence with its citations cut out. A narrative citation ("Boden (2009) notes") leaves its names, so the
    sentence still reads as written (D277); a parenthesis goes whole. Overlapping spans become one cut."""
    spans = {(*c.span, _kept_name(c) if keep_names and c.narrative else "") for c in found}
    cuts: list[tuple[int, int, str]] = []
    for start, end, keep in sorted(spans):
        if cuts and start < cuts[-1][1]:
            cuts[-1] = (cuts[-1][0], max(cuts[-1][1], end), "")
        else:
            cuts.append((start, end, keep))
    for start, end, keep in reversed(cuts):
        sentence = sentence[:start] + keep + sentence[end:]
    return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(sentence.split())).strip()


def _prose(text: str) -> str:
    """Drops "See [[note]]" pointers and turns other wikilinks into words."""
    text = _WIKILINK.sub(lambda m: m.group(2) or m.group(1).replace("-", " "), _SEE.sub("", text))
    text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", " ".join(text.split()))  # "et al." ending a sentence
    return re.sub(r"\s+([.,;:!?])", r"\1", text).strip()


def _row_claims(block: claim_text.Block) -> list[Cited]:
    """A table row's citation is checked against its own cell (the citation cut out), then against the row's cells
    that cite nothing, as prose. With two or more cited cells each gets only its own: another cited cell is another
    paper's finding."""
    cited_cells = sum(1 for cell in block.cells if citations.find(cell))
    uncited = [_prose(cell).rstrip(".") for cell in block.cells if not citations.find(cell)]
    out = []
    for cell in block.cells:
        if not (found := citations.find(cell)):
            continue
        bare = _without(cell, found, keep_names=False)
        own = _prose(_without(cell, found)).rstrip(".") if any(ch.isalnum() for ch in bare) else ""
        parts = [own] + ([] if cited_cells > 1 else uncited)
        claim = ". ".join(part for part in parts if part)
        if cited_cells > 1 and len(claim.split()) < 3:
            claim = ""
        out += [Cited(f"{claim}." if claim else "", c) for c in found if not c.superseded]
    return out


def _uncited_run(sentences: list[str]) -> list[str]:
    """The sentences up to the first one that carries its own citation."""
    run = []
    for sentence in sentences:
        if citations.find(sentence):
            break
        run.append(sentence)
    return run


def _block_claims(block: claim_text.Block) -> list[Cited]:
    """Each citation is checked against its own sentence (D277). Only a label the source marked (the block opens with
    a bold span that is that first sentence: "**Smith (2020).** …") hands its citation the rest of the block; a
    sentence that is only a citation otherwise has no claim. A cross-reference is not_a_claim."""
    sentences = citations.split_sentences(block.text)
    out = []
    for i, sentence in enumerate(sentences):
        if not (found := citations.find(sentence)):
            continue
        bare = _without(sentence, found, keep_names=False)
        if _CROSS_REFERENCE.search(bare):
            out += [Cited(_prose(_without(sentence, found)), c, "not_a_claim") for c in found if not c.superseded]
            continue
        if i == 0 and block.lead and sentence == block.lead:
            claim = _prose(" ".join(_uncited_run(sentences[1:])))
        elif not any(ch.isalnum() for ch in bare):  # nothing but the citation
            claim = ""
        else:
            claim = _prose(_without(sentence, found))
        if claim and claim[-1].isalnum():
            claim += "."
        out += [Cited(claim, c) for c in found if not c.superseded]
    return out


def cited_claims(body: str) -> list[Cited]:
    """Every citation in a note's body (markdown or plain text) with the claim it supports, in order (D276)."""
    return [c for b in claim_text.blocks(body) for c in (_row_claims(b) if b.cells else _block_claims(b))]


def _entry_for(entries: dict, citation: citations.Citation) -> tuple[list[RefEntry], RefEntry | None]:
    """The reference entries for a citation's key, narrowed by the co-authors it names; the one left, if one."""
    found = entries.get((citations.fold(citation.surname), citation.year), [])
    wanted = [citations.fold(c) for c in citation.coauthors]
    narrowed = [e for e in found if all(any(s.endswith(w) for s in e.surnames) for w in wanted)] if wanted else found
    return narrowed, (narrowed[0] if len(narrowed) == 1 else None)


async def _match(session, citation: citations.Citation, doi: str | None, workspace_id) -> citations.Match:
    """The note's reference list first: its DOI picks the one paper even when the library holds duplicates or the
    paper has no authors or year. Author–year when the list gives none or the DOI isn't in the library, but then
    never a paper whose own DOI differs from the list's."""
    if doi:
        by_doi = await citations.match(session, replace(citation, kind="doi", doi=doi), workspace_id)
        if len(by_doi.paper_ids) == 1:
            return by_doi
    return await citations.match(session, citation, workspace_id, only_doi=doi)


async def verify_claims(
    session: AsyncSession,
    text: str,
    *,
    workspace_id: uuid.UUID | None = None,
    llm=None,
    save: bool = False,
) -> list[ClaimItem]:
    """Checks every cited claim in `text`, in order: one item per citation, its claim the sentence, list item or
    table row that carries it (markdown is read as markdown). Send the whole note: a reference list at the end is never
    checked, but its DOIs are used to find the cited papers. A
    sentence that only points to another note is `not_a_claim`, neither matched nor checked.
    A citation that matches no paper is `citation_not_found`, several is `ambiguous_citation` with the
    titles; neither is guessed. A check that fails for one claim (its error code, or `check_failed` when the
    model can't be reached) is that item's error and the rest still run. More than 30 cited claims
    raises InvalidInput("too_many_claims") before any model call. `workspace_id` limits matching to one
    workspace (the caller resolves a name with workspaces.by_name). A citation a year away from the only matching
    paper still matches, with warning year_mismatch and the library's year.
    """
    body, references = claim_text.split_references(text)
    entries = claim_text.reference_entries(references)
    pending = cited_claims(body)
    if len(pending) > MAX_CLAIMS:
        raise InvalidInput("too_many_claims", detail=f"{len(pending)} cited claims; the limit is {MAX_CLAIMS}.")
    items = []
    for cited in pending:
        claim, citation = cited.claim, cited.citation
        if cited.error:
            items.append(ClaimItem(claim, citation, error=cited.error))
            continue
        narrowed, entry = _entry_for(entries, citation) if citation.kind == "author_year" else ([], None)
        if len(narrowed) > 1:
            # entries still share this key: the citation can't say which paper it means
            candidates = await citations.match(session, citation, workspace_id)
            items.append(ClaimItem(claim, citation, error="ambiguous_citation", candidates=candidates.titles))
            continue
        found = await _match(session, citation, entry.doi if entry else None, workspace_id)
        extra = {"warning": "year_mismatch", "library_year": found.library_year} if found.library_year else {}
        if not found.paper_ids and entry:
            items.append(ClaimItem(claim, citation, error="paper_not_in_library", doi=entry.doi))
        elif not found.paper_ids:
            items.append(ClaimItem(claim, citation, error="citation_not_found"))
        elif len(found.paper_ids) > 1:
            items.append(ClaimItem(claim, citation, error="ambiguous_citation", candidates=found.titles))
        elif len(claim.split()) < 3:
            items.append(ClaimItem(claim, citation, error="empty_claim", **extra))
        else:
            llm = await _llm(session, llm)  # no default model fails the whole batch, once, before any check
            try:
                check = await verify_claim(session, claim, paper_id=found.paper_ids[0], llm=llm, save=save)
            except DomainError as exc:
                items.append(ClaimItem(claim, citation, error=str(exc) or "check_failed", **extra))
            except (LLMError, LLMUnavailable):
                items.append(ClaimItem(claim, citation, error="check_failed", **extra))
            else:
                items.append(ClaimItem(claim, citation, check=check, **extra))
    return items
