"""Notes and their anchors. The provenance rules live here and nowhere else:

- LLM responses go to llm_outputs, never directly into notes.            (core/chat.py)
- Promoting an LLM fragment creates a note with provenance='llm' + source_id. (here)
- Saving a chat suggestion creates a note with provenance='llm', source_id = the answer and source_block = the
  block's index.  (here)
- Editing an 'llm' note flips it to 'llm_edited'.                          (here)
- Changing a note's colour never changes its provenance.                     (here)
- Changing a note's papers never changes its provenance (D97).                (here)
- Notes created through MCP get provenance='llm' and source_id = an llm_outputs row of kind 'mcp'.  (here)
- Showing a chart in a note never changes its provenance, and a note made from a chart is the owner's
  ('human'): a chart holds no generated text, only the owner's choice of data.  (here)
"""

import asyncio
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chunking import join_lines
from app.core.errors import Conflict, InvalidInput, NotFound
from app.core.papers import get_paper, get_paper_file, list_chunks
from app.models import Chart, Chunk, LLMOutput, Note, Paper, Provenance, note_anchors, note_charts, note_papers
from app.providers.extraction import quote_rects

Rect = tuple[float, float, float, float]

DEFAULT_COLOR = "#facc15"
_HEX_COLOR = re.compile(r"#[0-9a-f]{6}")
# What an MCP client's quote and a chunk's text are compared as: typographic quotes and dashes made plain, soft
# hyphens dropped (then NFKC, collapsed whitespace, casefold).
_PLAIN = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        "­": None})  # fmt: skip
MCP_OUTPUT_KIND = "mcp"
QUOTE_NOT_FOUND_HINT = "Copy quoted_text exactly from one passage of this paper, as search_library returned it."
QUOTE_AMBIGUOUS_HINT = "This text appears more than once in the paper. Quote a longer stretch around it."
# A citation marker in a stored answer: [C3] is a passage, [N2] a note. Chat parses answers with the same pattern.
_CITATION = re.compile(r"\[([CN])(\d+)\]")
# A suggested note in a chat answer (D94): a line ":::note", the note, then a line ":::".
NOTE_OPEN, NOTE_CLOSE = ":::note", ":::"
STALE_CHUNK = "a cited chunk no longer exists; the paper was re-ingested, so ask again"


@dataclass(frozen=True)
class Anchor:
    paper_id: uuid.UUID
    page: int
    bbox: list[Rect]
    quoted_text: str


@dataclass(frozen=True)
class ChartRef:
    id: uuid.UUID
    title: str


@dataclass(frozen=True)
class NoteView:
    id: uuid.UUID
    body: str
    provenance: str
    color: str
    source_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    anchors: list[Anchor]
    paper_ids: list[uuid.UUID]  # its papers by title, then id (D95); a passage is only ever on one of them
    charts: list[ChartRef] = field(default_factory=list)


def edited_provenance(current: str) -> str:
    return Provenance.LLM_EDITED if current == Provenance.LLM else current


def normalize_quote(text: str) -> str:
    """Browser selections keep the PDF's line breaks ("trans-\\nfer"); store reading text."""
    return join_lines(text.splitlines())


def normalize_color(color: str) -> str:
    value = color.strip().lower()
    if not _HEX_COLOR.fullmatch(value):
        raise InvalidInput(f"colour {color!r} is not a #rrggbb hex value")
    return value


def parse_citations(text: str, n: int, kind: str = "C") -> list[int]:
    """[C{i}] (or [N{i}]) numbers cited in the final text: first-seen order, no repeats, unknown labels dropped."""
    cited = (int(number) for label, number in _CITATION.findall(text) if label == kind)
    return list(dict.fromkeys(i for i in cited if 1 <= i <= n))


def note_blocks(content: str) -> list[str]:
    """The suggested notes in a chat answer, in order: blocks numbered from 0, empty ones included, so an index never
    shifts. A line that is exactly `:::note` once stripped opens a block and one that is exactly `:::` closes it; the
    block is the lines between, joined and stripped as a whole. A block still open at the end runs to the end, and a
    `:::note` line inside a block is text (they don't nest). Lines split on "\\n" only, so features/chat/noteBlocks.ts
    reads an answer the same way (both are tested on its noteBlocks.cases.json)."""
    blocks: list[str] = []
    lines: list[str] | None = None  # the open block's lines; None outside a block
    for line in content.split("\n"):
        if lines is None:
            if line.strip() == NOTE_OPEN:
                lines = []
        elif line.strip() == NOTE_CLOSE:
            blocks.append("\n".join(lines).strip())
            lines = None
        else:
            lines.append(line)
    if lines is not None:
        blocks.append("\n".join(lines).strip())
    return blocks


def reading_position(note: NoteView, paper_id: uuid.UUID) -> tuple[int, float, float]:
    return min((a.page, r[1], r[0]) for a in note.anchors if a.paper_id == paper_id for r in a.bbox)


async def _get_note(session: AsyncSession, note_id: uuid.UUID) -> Note:
    note = await session.get(Note, note_id)
    if note is None:
        raise NotFound(f"note {note_id} not found")
    return note


async def _link(session: AsyncSession, note_id: uuid.UUID, paper_ids) -> None:
    """Links the note to these papers (D95), each once; a link it has already is kept. Every writer calls it before it
    inserts anchors: note_anchors references note_papers, so an anchor on an unlinked paper is refused."""
    rows = [{"note_id": note_id, "paper_id": paper_id} for paper_id in dict.fromkeys(paper_ids)]
    if rows:
        await session.execute(pg_insert(note_papers).values(rows).on_conflict_do_nothing())


async def papers_of(session: AsyncSession, note_ids) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Each note's papers (D95), by title then id: what "a note's first paper" means everywhere. A note with no paper
    is absent. Sorted here, as workspaces.notes and the frontend sort, not by the database's collation."""
    rows = await session.execute(
        select(note_papers.c.note_id, Paper.id, Paper.title)
        .join(Paper, Paper.id == note_papers.c.paper_id)
        .where(note_papers.c.note_id.in_(list(note_ids)))
    )
    by_note: dict[uuid.UUID, list[tuple[str, str, uuid.UUID]]] = {}
    for note_id, paper_id, title in rows:
        by_note.setdefault(note_id, []).append((title, str(paper_id), paper_id))
    return {note_id: [paper_id for *_, paper_id in sorted(papers)] for note_id, papers in by_note.items()}


async def _with_anchors(session: AsyncSession, notes: list[Note]) -> list[NoteView]:
    """The notes as views: their passages, their papers and the charts they show."""
    rows = await session.execute(select(note_anchors).where(note_anchors.c.note_id.in_([n.id for n in notes])))
    anchors: dict[uuid.UUID, list[Anchor]] = {}
    for row in rows:
        anchor = Anchor(
            paper_id=row.paper_id,
            page=row.page,
            bbox=[tuple(r) for r in row.bbox],
            quoted_text=row.quoted_text or "",
        )
        anchors.setdefault(row.note_id, []).append(anchor)
    linked = await papers_of(session, [n.id for n in notes])
    shown = await session.execute(
        select(note_charts.c.note_id, Chart.id, Chart.title)
        .join(Chart, Chart.id == note_charts.c.chart_id)
        .where(note_charts.c.note_id.in_([n.id for n in notes]))
        .order_by(Chart.title)
    )
    charts: dict[uuid.UUID, list[ChartRef]] = {}
    for note_id, chart_id, title in shown:
        charts.setdefault(note_id, []).append(ChartRef(chart_id, title))
    return [
        NoteView(
            id=n.id,
            body=n.body,
            provenance=n.provenance,
            color=n.color,
            source_id=n.source_id,
            created_at=n.created_at,
            updated_at=n.updated_at,
            anchors=anchors.get(n.id, []),
            paper_ids=linked.get(n.id, []),
            charts=charts.get(n.id, []),
        )
        for n in notes
    ]


async def create_human_note(session: AsyncSession, body: str, anchor: Anchor, color: str = DEFAULT_COLOR) -> NoteView:
    paper = await get_paper(session, anchor.paper_id)
    if paper.page_count is not None and not 1 <= anchor.page <= paper.page_count:
        raise InvalidInput(f"page {anchor.page} is outside 1..{paper.page_count}")
    if not anchor.bbox:
        raise InvalidInput("an anchor needs at least one rect")
    quote = normalize_quote(anchor.quoted_text)
    if not quote:
        raise InvalidInput("an anchor needs the quoted text")

    note = Note(body=body.strip(), provenance=Provenance.HUMAN, color=normalize_color(color))
    session.add(note)
    await session.flush()
    await _link(session, note.id, [anchor.paper_id])
    await session.execute(
        insert(note_anchors).values(
            note_id=note.id,
            paper_id=anchor.paper_id,
            page=anchor.page,
            bbox=[list(r) for r in anchor.bbox],
            quoted_text=quote,
        )
    )
    await session.commit()
    await session.refresh(note)
    return (await _with_anchors(session, [note]))[0]


def _placed_on(note: NoteView, paper_id: uuid.UUID) -> bool:
    return any(anchor.paper_id == paper_id for anchor in note.anchors)


async def list_notes_for_paper(session: AsyncSession, paper_id: uuid.UUID) -> list[NoteView]:
    """The notes linked to the paper (D95): those with no passage on it first, newest first; then the rest in reading
    order. Raises NotFound."""
    await get_paper(session, paper_id)
    linked_here = select(note_papers.c.note_id).where(note_papers.c.paper_id == paper_id)
    views = await _with_anchors(session, list(await session.scalars(select(Note).where(Note.id.in_(linked_here)))))
    whole = sorted((v for v in views if not _placed_on(v, paper_id)), key=lambda v: v.created_at, reverse=True)
    placed = sorted((v for v in views if _placed_on(v, paper_id)), key=lambda v: reading_position(v, paper_id))
    return whole + placed


async def list_notes(
    session: AsyncSession, paper_id: uuid.UUID | None = None, unlinked: bool = False
) -> list[NoteView]:
    """Newest first (D98): every note; with `paper_id`, the notes linked to that paper (NotFound when there is no such
    paper); with `unlinked`, the notes linked to no paper."""
    query = select(Note).order_by(Note.created_at.desc(), Note.id)
    if paper_id is not None:
        await get_paper(session, paper_id)
        query = query.where(Note.id.in_(select(note_papers.c.note_id).where(note_papers.c.paper_id == paper_id)))
    elif unlinked:
        query = query.where(Note.id.not_in(select(note_papers.c.note_id)))
    return await _with_anchors(session, list(await session.scalars(query)))


async def set_papers(session: AsyncSession, note_id: uuid.UUID, paper_ids: list[uuid.UUID]) -> NoteView:
    """Links the note to exactly these papers (D95). A link not listed goes, and the note's passages on that paper go
    with it (the database cascades); a new link is to the whole paper. Duplicates count once. Provenance never changes
    (D97), and updated_at moves only when the set does. Raises NotFound (the note), NotFound("unknown_paper")."""
    note = await _get_note(session, note_id)
    wanted = set(paper_ids)
    if set(await session.scalars(select(Paper.id).where(Paper.id.in_(list(wanted))))) != wanted:
        raise NotFound("unknown_paper")
    current = set(await session.scalars(select(note_papers.c.paper_id).where(note_papers.c.note_id == note_id)))
    if current != wanted:
        gone = note_papers.c.paper_id.not_in(list(wanted))  # an empty list takes the note off every paper
        await session.execute(delete(note_papers).where(note_papers.c.note_id == note_id, gone))
        await _link(session, note_id, wanted - current)
        await session.execute(update(Note).where(Note.id == note_id).values(updated_at=func.now()))
        await session.commit()
    return await _view(session, note)


async def update_note(
    session: AsyncSession, note_id: uuid.UUID, *, body: str | None = None, color: str | None = None
) -> NoteView:
    note = await _get_note(session, note_id)
    changes: dict[str, str] = {}
    if body is not None and body.strip() != note.body:
        changes.update(body=body.strip(), provenance=edited_provenance(note.provenance))
    if color is not None and normalize_color(color) != note.color:
        changes["color"] = normalize_color(color)
    if changes:
        await session.execute(update(Note).where(Note.id == note_id).values(**changes, updated_at=func.now()))
        await session.commit()
        await session.refresh(note)
    return (await _with_anchors(session, [note]))[0]


async def delete_note(session: AsyncSession, note_id: uuid.UUID) -> None:
    await _get_note(session, note_id)
    await session.execute(delete(Note).where(Note.id == note_id))
    await session.commit()


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


async def _chunk_anchors(session: AsyncSession, chunk_ids: list[uuid.UUID]) -> list[dict]:
    """One anchor row (no note_id yet) per spot the chunks cover, in their order: note_anchors' PK is (note_id,
    paper_id, page, bbox), and two chunks can share a spot. chunk_id stays NULL because a re-ingest replaces chunks
    (D10). Raises InvalidInput(STALE_CHUNK) when a chunk is gone."""
    wanted = list(dict.fromkeys(chunk_ids))
    if not wanted:
        return []
    chunks = {c.id: c for c in await session.scalars(select(Chunk).where(Chunk.id.in_(wanted)))}
    if len(chunks) != len(wanted):
        raise InvalidInput(STALE_CHUNK)
    rows: dict[tuple, dict] = {}
    for chunk_id in wanted:
        c = chunks[chunk_id]
        key = (c.paper_id, c.page, tuple(tuple(rect) for rect in c.bbox))
        rows.setdefault(key, {"paper_id": c.paper_id, "page": c.page, "bbox": c.bbox, "quoted_text": c.text})
    return list(rows.values())


async def promote_llm_fragment(
    session: AsyncSession, output_id: uuid.UUID, body: str, chunk_ids: list[uuid.UUID]
) -> NoteView:
    """Save part of a stored answer as a note with provenance='llm' and source_id = the answer.

    The body must come from the answer, so text a person wrote can never be labelled as AI output.
    One anchor per spot the cited chunks cover (_chunk_anchors, shared with save_suggestion).
    """
    output = await session.get(LLMOutput, output_id)
    if output is None:
        raise NotFound(f"answer {output_id} not found")
    text = body.strip()
    if not text:
        raise InvalidInput("a promoted note needs the selected text")
    if _collapse_whitespace(text) not in _collapse_whitespace(output.content):
        raise InvalidInput("body_not_in_output")
    wanted = list(dict.fromkeys(chunk_ids))
    if not wanted:
        raise InvalidInput("a promoted note needs at least one cited chunk")
    if not set(wanted) <= set(output.source_chunks):
        raise InvalidInput("a chunk is not a source of this answer")
    rows = await _chunk_anchors(session, wanted)

    note = Note(body=text, provenance=Provenance.LLM, source_id=output.id)
    session.add(note)
    await session.flush()
    await _link(session, note.id, [row["paper_id"] for row in rows])
    await session.execute(insert(note_anchors), [{**row, "note_id": note.id} for row in rows])
    await session.commit()
    await session.refresh(note)
    return (await _with_anchors(session, [note]))[0]


async def save_suggestion(session: AsyncSession, output_id: uuid.UUID, index: int) -> NoteView:
    """Saves the chat answer's `:::note` block `index` as a note (D94, D96): provenance='llm', source_id = the answer,
    source_block = the index. The body is the block as stored, markers kept, so it is always the model's words.

    Anchored on the passages the block itself cites ([N…] anchors nothing), and linked to their papers, plus the
    answer's paper in paper chat (N6). Raises NotFound("answer_not_found"), InvalidInput("no_such_block" | "empty_body"
    | STALE_CHUNK), Conflict("already_saved").
    """
    output = await session.get(LLMOutput, output_id)
    if output is None or output.kind != "chat":
        raise NotFound("answer_not_found")
    blocks = note_blocks(output.content)
    if not 0 <= index < len(blocks):
        raise InvalidInput("no_such_block")
    body = blocks[index]
    if not body:
        raise InvalidInput("empty_body")
    cited = [output.source_chunks[i - 1] for i in parse_citations(body, len(output.source_chunks))]
    rows = await _chunk_anchors(session, cited)
    papers = [row["paper_id"] for row in rows] + ([output.paper_id] if output.paper_id else [])

    note = Note(body=body, provenance=Provenance.LLM, source_id=output_id, source_block=index)
    session.add(note)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        # Name the refusal only when it is the unique index: another insert failure (the answer deleted meanwhile)
        # is not "already saved".
        saved = select(Note.id).where(Note.source_id == output_id, Note.source_block == index)
        if await session.scalar(saved) is None:
            raise
        raise Conflict("already_saved") from None
    await _link(session, note.id, papers)
    if rows:
        await session.execute(insert(note_anchors), [{**row, "note_id": note.id} for row in rows])
    await session.commit()
    return await _view(session, note)


async def _get_chart(session: AsyncSession, chart_id: uuid.UUID) -> Chart:
    chart = await session.get(Chart, chart_id)
    if chart is None:
        raise NotFound(f"chart {chart_id} not found")
    return chart


async def _view(session: AsyncSession, note: Note) -> NoteView:
    await session.refresh(note)
    return (await _with_anchors(session, [note]))[0]


async def attach_chart(session: AsyncSession, note_id: uuid.UUID, chart_id: uuid.UUID) -> NoteView:
    """Shows a chart in a note. Attaching twice is a no-op."""
    note = await _get_note(session, note_id)
    await _get_chart(session, chart_id)
    await session.execute(pg_insert(note_charts).values(note_id=note_id, chart_id=chart_id).on_conflict_do_nothing())
    await session.execute(update(Note).where(Note.id == note_id).values(updated_at=func.now()))
    await session.commit()
    return await _view(session, note)


async def detach_chart(session: AsyncSession, note_id: uuid.UUID, chart_id: uuid.UUID) -> NoteView:
    """Stops showing a chart in a note. Detaching a chart the note doesn't show is a no-op."""
    note = await _get_note(session, note_id)
    removed = await session.execute(
        delete(note_charts).where(note_charts.c.note_id == note_id, note_charts.c.chart_id == chart_id)
    )
    if removed.rowcount:
        await session.execute(update(Note).where(Note.id == note_id).values(updated_at=func.now()))
    await session.commit()
    return await _view(session, note)


async def create_chart_note(session: AsyncSession, chart_id: uuid.UUID, anchors: list[Anchor]) -> NoteView:
    """An empty 'human' note showing the chart, anchored where its data sits in each paper, so it appears in every
    source paper's notes. The anchors come from core/charts.py's chart_anchors."""
    await _get_chart(session, chart_id)
    if not anchors:
        raise InvalidInput("a note needs at least one anchor")
    note = Note(body="", provenance=Provenance.HUMAN)
    session.add(note)
    await session.flush()
    # note_anchors' PK is (note_id, paper_id, page, bbox): one anchor per spot.
    rows: dict[tuple, dict] = {}
    for a in anchors:
        key = (a.paper_id, a.page, tuple(tuple(r) for r in a.bbox))
        rows.setdefault(key, {"note_id": note.id, "paper_id": a.paper_id, "page": a.page,
                              "bbox": [list(r) for r in a.bbox], "quoted_text": a.quoted_text})  # fmt: skip
    await _link(session, note.id, [row["paper_id"] for row in rows.values()])
    await session.execute(insert(note_anchors), list(rows.values()))
    await session.execute(insert(note_charts).values(note_id=note.id, chart_id=chart_id))
    await session.commit()
    return await _view(session, note)


def fold(text: str) -> str:
    """Text as a quote is matched: NFKC, plain quotes and dashes, no soft hyphens, collapsed whitespace, casefolded."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_PLAIN).split()).casefold()


def quote_hits(chunk_text: str, quote: str) -> list[list[int]]:
    """Every occurrence of a folded quote in a chunk, as the indexes of the blocks it touches.

    A chunk's text is its blocks joined by a blank line and its bbox holds one rect per block (core/chunking.py), so
    block i of the text is rect i of the bbox.
    """
    blocks = [fold(block) for block in chunk_text.split("\n\n")]
    spans, start = [], 0
    for block in blocks:
        spans.append((start, start + len(block)))
        start += len(block) + 1
    joined, hits = " ".join(blocks), []
    at = joined.find(quote)
    while at != -1:
        end = at + len(quote)
        hits.append([i for i, (first, last) in enumerate(spans) if first < end and at < last])
        at = joined.find(quote, at + 1)
    return hits


async def locate_quote(session: AsyncSession, paper_id: uuid.UUID, quoted_text: str) -> Anchor:
    """Where a quote sits in a paper: its page and rects, for a caller that knows only the words (MCP, Q1).

    The quote must occur exactly once in the paper's chunks. Its rects are the quoted lines when the PDF is on this
    machine, else the paragraphs they're in. Raises NotFound (the paper), InvalidInput("empty_quote" |
    "quote_not_found" with a hint | "quote_ambiguous" with pages and a hint), Conflict("paper_not_ready") when the
    paper has no chunks yet.
    """
    chunks = await list_chunks(session, paper_id)
    quote = fold(quoted_text)
    if not quote:
        raise InvalidInput("empty_quote")
    if not chunks:
        raise Conflict("paper_not_ready")
    # ponytail: a quote that crosses from one chunk into the next isn't found; the hint asks for one passage. Join
    # neighbouring chunks on a page if models keep quoting across them.
    # A place is a page and the paragraphs a hit touches. Chunks of a paper without headings repeat a paragraph at
    # the start of the next chunk, so one place can be hit from two chunks: each place counts as often as the one
    # chunk that hits it most (Counter's | keeps the larger count), so a sentence twice in one paragraph is still two.
    places: Counter[tuple[int, tuple[Rect, ...]]] = Counter()
    for chunk in chunks:
        hits = quote_hits(chunk.text, quote)
        places |= Counter((chunk.page, tuple(tuple(chunk.bbox[i]) for i in blocks)) for blocks in hits)
    if not places:
        raise InvalidInput("quote_not_found", hint=QUOTE_NOT_FOUND_HINT)
    if places.total() > 1:
        raise InvalidInput("quote_ambiguous", pages=sorted({page for page, _ in places}), hint=QUOTE_AMBIGUOUS_HINT)
    [(page, rects)] = places
    paragraphs = list(rects)
    try:
        path = await get_paper_file(session, paper_id)
    except NotFound:  # the PDF isn't on this machine: the paragraphs will do
        lines = []
    else:
        lines = await asyncio.to_thread(quote_rects, path, page, paragraphs, quoted_text)
    return Anchor(paper_id, page, lines or paragraphs, normalize_quote(quoted_text))


async def create_llm_note(session: AsyncSession, paper_id: uuid.UUID, body: str, quoted_text: str) -> NoteView:
    """A note an MCP client wrote, anchored on `quoted_text`: provenance='llm', with its text also kept as an
    llm_outputs row (kind 'mcp') the note's source_id points at, so the model's words survive an edit (D91).

    Raises InvalidInput("empty_body"), and whatever locate_quote raises.
    """
    text = body.strip()
    if not text:
        raise InvalidInput("empty_body")
    anchor = await locate_quote(session, paper_id, quoted_text)
    # ponytail: model and prompt_version are NOT NULL, but an MCP client names no PaperLab model and uses no prompt.
    output = LLMOutput(paper_id=paper_id, kind=MCP_OUTPUT_KIND, content=text, model="mcp", prompt_version=0)
    session.add(output)
    await session.flush()
    return await _add_llm_note(session, paper_id, text, output.id, anchor)


async def create_ai_note(
    session: AsyncSession, paper_id: uuid.UUID, body: str, output_id: uuid.UUID, quoted_text: str | None = None
) -> NoteView:
    """A note PaperLab's own model wrote (a claim check): provenance='llm', source_id = the llm_outputs row it came
    from (no second row). Anchored when `quoted_text` locates in the paper, else just linked to it.

    Raises InvalidInput("empty_body").
    """
    text = body.strip()
    if not text:
        raise InvalidInput("empty_body")
    anchor = None
    if quoted_text:
        try:
            anchor = await locate_quote(session, paper_id, quoted_text)
        except (InvalidInput, Conflict):
            pass  # a quote spanning chunks, or an abstract-only paper: keep the note, drop the anchor
    return await _add_llm_note(session, paper_id, text, output_id, anchor)


async def _add_llm_note(
    session: AsyncSession, paper_id: uuid.UUID, text: str, output_id: uuid.UUID, anchor: Anchor | None
) -> NoteView:
    note = Note(body=text, provenance=Provenance.LLM, source_id=output_id)
    session.add(note)
    await session.flush()
    await _link(session, note.id, [paper_id])
    if anchor:
        await session.execute(
            insert(note_anchors).values(
                note_id=note.id,
                paper_id=paper_id,
                page=anchor.page,
                bbox=[list(rect) for rect in anchor.bbox],
                quoted_text=anchor.quoted_text,
            )
        )
    await session.commit()
    return await _view(session, note)
