"""Evidence cards (M39 F5): a structured, quote-backed summary of one paper. One retrieval per field, one model call,
and every item checked: its quote must be a verbatim passage fragment (core/quotes) and every number in its values
must appear in that quote (core/numbers). An item that fails either check is never shown as a fact."""

import copy
import csv
import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, BeforeValidator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import claims, llm_json, numbers, prompts, queue, quotes, retrieval
from app.core.errors import Conflict, InvalidInput, NotFound
from app.models import Chunk, EvidenceCard, LLMOutput, Paper, PaperStatus, Workspace, workspace_papers

CARD_PROMPT_VERSION = 2
JOB_TIMEOUT = 20 * 60  # seconds; the job's registered timeout. A card `generating` longer than this is stale.
OUTPUT_KIND = "evidence_card"
PER_FIELD = 4
MAX_PASSAGES = 16
LEAD_CHUNKS = 2  # the abstract and opening: where the question, sample and headline effect are stated
CAPS = {"findings": 5, "effect_sizes": 8, "limitations": 5}
SYSTEM_PROMPT, PROMPT_TEMPLATE = prompts.load("evidence_card", CARD_PROMPT_VERSION).split("\n<!-- prompt -->\n")
FIELD_QUERIES = {
    "research_question": "research question aims objectives hypotheses",
    "design": "method design procedure participants materials",
    "sample": "sample participants N recruited population demographics",
    "findings": "results findings showed significant",
    "effect_sizes": "effect size Cohen's d odds ratio confidence interval p value",
    "limitations": "limitations future work caveats generalizability",
}


def _text(value):
    return None if value is None else str(value)  # a model often writes 0.001 or 120 where a string is asked


def _cap(limit: int):
    return BeforeValidator(lambda v: v[:limit] if isinstance(v, list) else v)


Text = Annotated[str | None, BeforeValidator(_text)]


class _Item(BaseModel):
    value: Text = None
    passage: int | None = None
    quote: str = ""


class _Sample(BaseModel):
    n: int | None = None
    population: Text = None
    passage: int | None = None
    quote: str = ""


class _Effect(BaseModel):
    value: Text = None
    statistic: Text = None
    ci: Text = None
    p: Text = None
    outcome: Text = None
    passage: int | None = None
    quote: str = ""


class _CardReply(BaseModel):
    research_question: _Item | None = None
    design: _Item | None = None
    sample: _Sample | None = None
    findings: Annotated[list[_Item], _cap(CAPS["findings"])] = []
    effect_sizes: Annotated[list[_Effect], _cap(CAPS["effect_sizes"])] = []
    limitations: Annotated[list[_Item], _cap(CAPS["limitations"])] = []


# Per item type: its stored value keys, and the ones whose numbers must be in the quote.
_SHAPES = {
    _Item: (("value",), ("value",)),
    _Sample: (("n", "population"), ("n", "population")),
    _Effect: (("value", "statistic", "ci", "p", "outcome"), ("value", "statistic", "ci", "p", "outcome")),
}


@dataclass(frozen=True)
class CardView:
    paper_id: uuid.UUID
    status: str  # generating | ready | failed
    error: str | None = None  # not_ready | no_text | model_error
    fields: dict = field(default_factory=dict)
    provenance: str = "llm"
    model: str | None = None
    prompt_version: int | None = None
    output_id: uuid.UUID | None = None
    updated_at: datetime | None = None


def _blank(item_type, flags: list[str]) -> dict:
    keys = _SHAPES[item_type][0]
    return {**dict.fromkeys(keys), "quote": None, "page": None, "chunk_id": None, "flags": flags}


def _verified(item, passages: list[dict], texts: list[str]) -> dict | None:
    """The kept item with its page and chunk, a blank item carrying the flag that dropped it, or None when the model
    claimed nothing."""
    keys, checked = _SHAPES[type(item)]
    data = {k: getattr(item, k) for k in keys}
    if all(v in (None, "") for v in data.values()):
        return None
    found = quotes.locate(item.quote, item.passage, texts) if item.quote.strip() else None
    if found is None:
        return _blank(type(item), ["unsupported"])
    quote, index = found
    if type(item) is _Effect and not numbers.tokens(str(item.value or "")):
        return _blank(type(item), ["no_statistic"])
    if not all(numbers.all_in(str(data[k]), quote) for k in checked if data[k] is not None):
        return _blank(type(item), ["number_not_in_source"])
    chunk_id = passages[index]["chunk_id"]
    return {**data, "quote": quote, "page": passages[index]["page"], "chunk_id": str(chunk_id), "flags": []}


def _fields(reply: _CardReply, passages: list[dict], texts: list[str]) -> dict:
    out = {}
    for name, item_type in (("research_question", _Item), ("design", _Item), ("sample", _Sample)):
        out[name] = _verified(getattr(reply, name), passages, texts) if getattr(reply, name) else None
        out[name] = out[name] or _blank(item_type, [])
    for name in CAPS:
        out[name] = [v for item in getattr(reply, name) if (v := _verified(item, passages, texts))]
    return out


async def _passages(session: AsyncSession, paper_id: uuid.UUID) -> tuple[list[dict], list[str]]:
    """The opening chunks, then the best chunks per field interleaved so no field starves; deduped and capped."""
    per_field = [
        await retrieval.retrieve(session, query, paper_ids=[paper_id], k=PER_FIELD) for query in FIELD_QUERIES.values()
    ]
    opening = await session.execute(
        select(Chunk).where(Chunk.paper_id == paper_id).order_by(Chunk.ordinal).limit(LEAD_CHUNKS)
    )
    chosen: dict[uuid.UUID, retrieval.RetrievedChunk] = {
        c.id: retrieval.RetrievedChunk(c.id, c.paper_id, c.page, c.section_title, c.bbox, c.text)
        for c in opening.scalars()
    }
    for rank in range(PER_FIELD):
        for found in per_field:
            if rank < len(found):
                chosen.setdefault(found[rank].id, found[rank])
    kept = list(chosen.values())[:MAX_PASSAGES]
    return [{"chunk_id": c.id, "page": c.page, "section": c.section} for c in kept], [c.text for c in kept]


def _prompt(passages: list[dict], texts: list[str]) -> str:
    numbered = "\n\n".join(
        f"[{i}] (page {p['page']}, section {p['section']}) {t}"
        for i, (p, t) in enumerate(zip(passages, texts, strict=True), start=1)
    )
    return PROMPT_TEMPLATE.format(passages=numbered)  # str.format only parses the template: braces in passages are safe


async def _write_output(session, paper_id, content: dict, passages, model, connection_name) -> uuid.UUID:
    output = LLMOutput(
        paper_id=paper_id, kind=OUTPUT_KIND, content=json.dumps(content),
        source_chunks=[p["chunk_id"] for p in passages], model=model, connection_name=connection_name,
        prompt_version=CARD_PROMPT_VERSION,
    )
    session.add(output)
    await session.commit()
    return output.id


async def _store(session: AsyncSession, view: CardView) -> CardView:
    """Upserts the paper's card. A failed generation keeps the fields of an earlier card."""
    card = await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == view.paper_id))
    if card is None:
        card = EvidenceCard(paper_id=view.paper_id, fields={})
        session.add(card)
    card.status, card.error = view.status, view.error
    if view.status == "ready":
        card.fields = view.fields
    card.provenance, card.model = view.provenance, view.model
    card.prompt_version, card.output_id = view.prompt_version, view.output_id
    card.updated_at = datetime.now(UTC)
    await session.commit()
    return CardView(
        view.paper_id, card.status, card.error, card.fields, card.provenance, card.model, card.prompt_version,
        card.output_id, card.updated_at,
    )


async def build_card(session: AsyncSession, paper_id: uuid.UUID, *, llm=None, record: bool = True) -> CardView:
    """Builds the paper's evidence card from its full text and (with `record`) stores it with an llm_outputs row.

    A paper that isn't ready is `failed` / `not_ready`, a ready one without chunks `failed` / `no_text`; neither
    asks the model. Two invalid model replies are `failed` / `model_error` (the row records the failure). With
    `record=False` nothing is written (for the eval) and the view has no output_id.
    """
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise NotFound("paper_not_found", detail=f"Paper {paper_id} does not exist.")
    failed = None
    if paper.status != PaperStatus.READY:
        failed = "not_ready"
    elif not await session.scalar(select(func.count()).select_from(Chunk).where(Chunk.paper_id == paper_id)):
        failed = "no_text"
    if failed:
        view = CardView(paper_id, "failed", failed)
        return await _store(session, view) if record else view
    passages, texts = await _passages(session, paper_id)
    llm = await claims._llm(session, llm)
    model, connection_name = getattr(llm, "model", "unknown"), getattr(llm, "connection_name", None)
    reply = await llm_json.ask_json(llm, SYSTEM_PROMPT, _prompt(passages, texts), _CardReply)
    if reply is None:
        view = CardView(paper_id, "failed", "model_error", model=model, prompt_version=CARD_PROMPT_VERSION)
        content = {"error": "model_error"}
    else:
        view = CardView(
            paper_id, "ready", fields=_fields(reply, passages, texts), model=model, prompt_version=CARD_PROMPT_VERSION
        )
        content = reply.model_dump()
    if not record:
        return view
    output_id = await _write_output(session, paper_id, content, passages, model, connection_name)
    return await _store(session, CardView(**{**view.__dict__, "output_id": output_id}))


def _view(card: EvidenceCard) -> CardView:
    return CardView(
        card.paper_id, card.status, card.error, card.fields, card.provenance, card.model, card.prompt_version,
        card.output_id, card.updated_at,
    )


async def request_cards(
    session: AsyncSession, *, paper_ids: list[uuid.UUID], workspace_id: uuid.UUID | None = None,
    regenerate: bool = False, confirm: bool = False,
) -> list[CardView]:
    """Marks each target paper's card `generating` and queues ONE background job for the new targets.

    A `ready` card is left alone unless `regenerate`; replacing an edited card also needs `confirm` (Conflict
    `card_edited_needs_confirm`, checked for every paper before anything changes). A card already `generating` is
    returned as is and not queued again, so two requests make one job. Returns one view per paper, in order.
    """
    paper_ids = list(dict.fromkeys(paper_ids))
    if not paper_ids:
        return []
    known = await session.scalar(select(func.count()).select_from(Paper).where(Paper.id.in_(set(paper_ids))))
    if known != len(set(paper_ids)):
        raise NotFound("paper_not_found")
    if regenerate and not confirm and await session.scalar(
        select(func.count()).select_from(EvidenceCard).where(
            EvidenceCard.paper_id.in_(paper_ids), EvidenceCard.status == "ready",
            EvidenceCard.provenance == "llm_edited",
        )
    ):
        raise Conflict("card_edited_needs_confirm")
    # Insert missing rows first (ON CONFLICT: a concurrent request may have just made one), then read them all.
    await session.execute(
        pg_insert(EvidenceCard).values([{"paper_id": i, "status": "failed", "fields": {}} for i in paper_ids])
        .on_conflict_do_nothing(index_elements=["paper_id"])
    )
    cards = {
        c.paper_id: c
        for c in await session.scalars(
            select(EvidenceCard).where(EvidenceCard.paper_id.in_(paper_ids)).execution_options(populate_existing=True)
        )
    }
    stale_before = datetime.now(UTC) - timedelta(seconds=JOB_TIMEOUT)
    queued = []
    for paper_id in paper_ids:
        card = cards[paper_id]
        if card.status == "ready" and not regenerate:
            continue
        if card.status == "generating" and card.updated_at > stale_before:
            continue  # a job has it (a stale one is a lost job: queue it again)
        card.status, card.error, card.updated_at = "generating", None, datetime.now(UTC)
        queued.append(paper_id)
    await session.commit()
    if queued:
        try:
            await queue.enqueue("generate_evidence_cards", None if workspace_id is None else str(workspace_id),
                                [str(i) for i in queued])
        except Exception:
            for paper_id in queued:  # no job will run: don't leave the cards `generating` until they go stale
                cards[paper_id].status, cards[paper_id].error = "failed", "queue_error"
            await session.commit()
            raise
    return [_view(cards[i]) for i in paper_ids]


_EDITABLE = {"research_question": _Item, "design": _Item, "sample": _Sample, "findings": _Item,
             "effect_sizes": _Effect, "limitations": _Item}


async def get_card(session: AsyncSession, paper_id: uuid.UUID) -> CardView:
    card = await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper_id))
    if card is None:
        raise NotFound("card_not_found", detail=f"Paper {paper_id} has no evidence card.")
    return _view(card)


def _edited(fields: dict, change: dict) -> dict:
    """A copy of `fields` with one change applied: {field, index (lists), values: {key: new value}}."""
    name, values = change.get("field"), change.get("values") or {}
    if name not in _EDITABLE:
        raise InvalidInput("unknown_field", fields=list(_EDITABLE))
    keys = _SHAPES[_EDITABLE[name]][0]
    if not values or set(values) - set(keys):
        raise InvalidInput("not_editable", editable=list(keys))  # quotes and pages are the evidence
    out = copy.deepcopy(fields)
    if name in CAPS:
        index = change.get("index", 0)
        if type(index) is not int or not 0 <= index < len(out[name]):
            raise InvalidInput("unknown_item", field=name, index=index)
        item = out[name][index]
    else:
        item = out[name]
    if not item.get("quote"):
        raise InvalidInput("item_has_no_evidence", field=name)  # a dropped item has nothing to back an edit
    if "n" in values and values["n"] is not None and type(values["n"]) is not int:
        raise InvalidInput("not_editable", editable=list(keys), detail="n must be a whole number")
    for key, value in values.items():
        item[key] = value if value is None else (value if key == "n" else str(value))
    item["flags"] = sorted({*item["flags"], "edited"})
    return out


async def edit_card(session: AsyncSession, paper_id: uuid.UUID, changes: list[dict]) -> CardView:
    """Changes item values (never quotes or pages), flags each `edited` and marks the card `llm_edited`."""
    card = await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper_id))
    if card is None:
        raise NotFound("card_not_found", detail=f"Paper {paper_id} has no evidence card.")
    if card.status != "ready":
        raise Conflict("card_not_ready")
    fields = card.fields
    for change in changes:
        fields = _edited(fields, change)
    if changes:
        card.fields, card.provenance, card.updated_at = fields, "llm_edited", datetime.now(UTC)
        await session.commit()
    return _view(card)


@dataclass(frozen=True)
class CardRow:
    paper_id: uuid.UUID
    title: str
    year: int | None
    first_author: str | None
    card: CardView | None


async def workspace_cards(session: AsyncSession, workspace_id: uuid.UUID) -> list[CardRow]:
    """One row per workspace paper (by title), with its card or None. NotFound for an unknown workspace."""
    if await session.get(Workspace, workspace_id) is None:
        raise NotFound("workspace_not_found", detail=f"Workspace {workspace_id} does not exist.")
    rows = await session.execute(
        select(Paper, EvidenceCard)
        .join(workspace_papers, workspace_papers.c.paper_id == Paper.id)
        .outerjoin(EvidenceCard, EvidenceCard.paper_id == Paper.id)
        .where(workspace_papers.c.workspace_id == workspace_id)
        .order_by(Paper.title, Paper.id)
    )
    return [
        CardRow(p.id, p.title, p.year, (p.authors or [None])[0], None if c is None else _view(c)) for p, c in rows
    ]


def _shown(name: str, item: dict) -> str:
    """The item's value as one cell, or "" when it holds none (a dropped or cleared item)."""
    if name == "sample":
        parts = [item.get("n"), item.get("population")]
    else:
        parts = [item.get("value"), item.get("ci"), item.get("p")] if name == "effect_sizes" else [item.get("value")]
    return "; ".join(str(v) for v in parts if v not in (None, ""))


@dataclass(frozen=True)
class CardSummary:
    """A card for Claude: only items that hold a value (each with its quote and page); dropped ones are counted."""

    paper_id: uuid.UUID
    status: str
    error: str | None
    provenance: str
    model: str | None
    prompt_version: int | None
    updated_at: datetime | None
    fields: dict
    left_out: dict  # field -> how many items had no usable quote or numbers (never shown as facts)


def summarize(view: CardView) -> CardSummary:
    kept, left_out = {}, {}
    for name in _EDITABLE:
        value = view.fields.get(name)
        items = value if isinstance(value, list) else [value] if value else []
        shown = [i for i in items if i.get("quote") and _shown(name, i)]
        kept[name] = shown if isinstance(value, list) else (shown[0] if shown else None)
        dropped = [i for i in items if not _shown(name, i) and {"unsupported", "number_not_in_source"} & {*i["flags"]}]
        if dropped:
            left_out[name] = len(dropped)
    return CardSummary(
        view.paper_id, view.status, view.error, view.provenance, view.model, view.prompt_version, view.updated_at,
        kept if view.status == "ready" else {}, left_out,
    )


def _cell(text) -> str:
    """Text for a CSV cell that a spreadsheet will not run as a formula."""
    text = str(text)
    return "'" + text if text[:1] in tuple("=+-@\t\r") else text


CSV_HEADER = ["paper", "year", "field", "value", "quote", "page", "author", "edited"]


async def workspace_csv(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    """Every kept item of every ready card in the workspace, one row each, UTF-8 BOM for Excel. Items without a
    value (dropped as unsupported, or cleared) are left out: they are not facts."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_HEADER)
    for row in await workspace_cards(session, workspace_id):
        if row.card is None or row.card.status != "ready":
            continue
        for name in _EDITABLE:
            value = row.card.fields.get(name)
            for item in value if isinstance(value, list) else [value]:
                if not item or not item.get("quote") or not (shown := _shown(name, item)):
                    continue
                writer.writerow([
                    _cell(row.title), row.year or "", name, _cell(shown), _cell(item["quote"]), item["page"] or "",
                    _cell(row.first_author or ""),
                    "yes" if "edited" in item["flags"] else "no",
                ])
    return "\ufeff" + out.getvalue()
