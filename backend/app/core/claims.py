"""Claim verification (M39 F4): is a claim supported by a library paper? One model call over the paper's best
passages, a verbatim quote that is checked against them, and an llm_outputs row for provenance."""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import citations, llm_connections, llm_json, notes, prompts, quotes, retrieval
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
    "supported": "Supported", "partially_supported": "Partly supported",
    "not_supported": "Not supported", "not_addressed": "Not addressed",
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


async def _passages(session, paper: Paper, claim: str, quoted_text: str | None) -> tuple[str, list[dict], list[str]]:
    """(evidence level, passage descriptions, passage texts): the paper's best chunks, else its abstract."""
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
            )
    if (paper.abstract or "").strip():
        return "abstract_only", [{"chunk_id": None, "page": None, "section": None}], [paper.abstract]
    return "metadata_only", [], []


def _prompt(claim: str, quoted_text: str | None, texts: list[str], passages: list[dict]) -> str:
    numbered = "\n\n".join(
        f"[{i}] (page {p['page']}, section {p['section']}) {t}" if p["page"] is not None else f"[{i}] (abstract) {t}"
        for i, (p, t) in enumerate(zip(passages, texts, strict=True), start=1)
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
        paper_id=paper.id, kind=OUTPUT_KIND, question=claim, content=json.dumps(content),
        source_chunks=[p["chunk_id"] for p in passages if p["chunk_id"] is not None],
        model=model, connection_name=connection_name, prompt_version=CLAIM_PROMPT_VERSION,
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
    session: AsyncSession, claim: str, *, paper_id: uuid.UUID | None = None, doi: str | None = None,
    quoted_text: str | None = None, llm=None, save: bool = False,
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
    level, passages, texts = await _passages(session, paper, claim, quoted_text)
    base = {"claim": claim, "paper_id": paper.id, "paper_title": paper.title, "evidence_level": level}
    if level == "metadata_only":
        if paper.status not in (PaperStatus.READY, PaperStatus.FAILED):  # still ingesting: text may come
            return ClaimCheck(**base, verdict="cannot_verify", reason="not_ready", paper_status=str(paper.status))
        return ClaimCheck(**base, verdict="cannot_verify", reason="no_text")
    llm = await _llm(session, llm)
    model = getattr(llm, "model", "unknown")
    connection_name = getattr(llm, "connection_name", None)
    reply = await llm_json.ask_json(llm, SYSTEM_PROMPT, _prompt(claim, quoted_text, texts, passages), _Reply)
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
        **base, verdict=reply.verdict, reason=reply.reason.strip(), quote=quote,
        quote_page=chosen.get("page"), quote_section=chosen.get("section"), quote_chunk_id=chosen.get("chunk_id"),
        passages=passages, confidence="low" if low else "high", model=model,
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
    error: str | None = None  # citation_not_found | ambiguous_citation | empty_claim
    candidates: list[str] = field(default_factory=list)  # titles when ambiguous


def _without(sentence: str, found: list[citations.Citation]) -> str:
    """The sentence with every citation span cut out and the spacing tidied."""
    merged: list[list[int]] = []
    for start, end in sorted({c.span for c in found}):  # overlapping spans (a DOI inside a parenthesis) become one
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        sentence = sentence[:start] + sentence[end:]
    sentence = " ".join(sentence.split())
    return re.sub(r"\s+([.,;:!?])", r"\1", sentence).strip()


async def verify_claims(
    session: AsyncSession,
    text: str,
    *,
    workspace_id: uuid.UUID | None = None,
    llm=None,
    save: bool = False,
) -> list[ClaimItem]:
    """Checks every cited claim in `text`, in order: one item per citation (a sentence citing two papers is two
    claims). A citation that matches no paper is `citation_not_found`, several is `ambiguous_citation` with the
    titles; neither is guessed. A check that fails for one claim (its error code, or `check_failed` when the
    model can't be reached) is that item's error and the rest still run. More than 30 cited claims
    raises InvalidInput("too_many_claims") before any model call. `workspace_id` limits matching to one
    workspace (the caller resolves a name with workspaces.by_name).
    """
    pending = [
        (_without(s, found), c) for s in citations.split_sentences(text) if (found := citations.find(s)) for c in found
    ]
    if len(pending) > MAX_CLAIMS:
        raise InvalidInput("too_many_claims", detail=f"{len(pending)} cited claims; the limit is {MAX_CLAIMS}.")
    items = []
    for claim, citation in pending:
        found = await citations.match(session, citation, workspace_id)
        if not found.paper_ids:
            items.append(ClaimItem(claim, citation, error="citation_not_found"))
        elif len(found.paper_ids) > 1:
            items.append(ClaimItem(claim, citation, error="ambiguous_citation", candidates=found.titles))
        elif len(claim.split()) < 3:
            items.append(ClaimItem(claim, citation, error="empty_claim"))
        else:
            llm = await _llm(session, llm)  # no default model fails the whole batch, once, before any check
            try:
                check = await verify_claim(session, claim, paper_id=found.paper_ids[0], llm=llm, save=save)
            except DomainError as exc:
                items.append(ClaimItem(claim, citation, error=str(exc) or "check_failed"))
            except (LLMError, LLMUnavailable):
                items.append(ClaimItem(claim, citation, error="check_failed"))
            else:
                items.append(ClaimItem(claim, citation, check=check))
    return items
