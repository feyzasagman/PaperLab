"""Markdown export of a workspace's notes for Obsidian (M39 F8, D259-D260): one file per paper, named by its F7 key."""

import io
import re
import uuid
import zipfile
from dataclasses import dataclass

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import notes, workspaces
from app.core.citations_export import bibtex_keys, csl_item, filename_slug
from app.core.errors import InvalidInput
from app.models import Note, Paper, Provenance, Workspace, note_anchors, note_papers

LABELS = {"human": "You", "llm": "AI", "llm_edited": "AI · edited"}
HEADING_MAX = 80


@dataclass(frozen=True)
class AnchorItem:
    paper_id: uuid.UUID
    page: int
    chunk_id: uuid.UUID | None
    quoted_text: str


@dataclass(frozen=True)
class NoteItem:
    body: str
    provenance: str
    paper_ids: list[uuid.UUID]
    anchors: list[AnchorItem]


def neutralise(text: str) -> str:
    """Text for a heading or wikilink label: `#` would start a tag, `[[ ]]` a link."""
    return re.sub(r"\]{2,}", "]", re.sub(r"\[{2,}", "[", text)).replace("#", "\\#")


def _frontmatter(paper, origin: str) -> str:
    item = csl_item(paper)
    authors = [a.get("literal") or f"{a['family']}, {a['given']}" for a in item.get("author", [])]
    meta = {
        "title": paper.title,
        "doi": paper.doi,
        "year": paper.year,
        "authors": authors,
        "source_type": paper.source_type,
        "paperlab": f"paperlab://papers/{paper.id}",
        "paperlab_web": f"{origin}/#/papers/{paper.id}",
        "tags": ["paperlab"],
    }
    dump = yaml.safe_dump({k: v for k, v in meta.items() if v}, sort_keys=False, allow_unicode=True)
    return f"---\n{dump}---\n"


def _heading(body: str) -> str:
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "Note")
    return neutralise(first.lstrip("#").strip() or "Note")[:HEADING_MAX]


def _block(paper_id: uuid.UUID, n: NoteItem, keys: dict[str, str]) -> str:
    here = sorted((a for a in n.anchors if a.paper_id == paper_id), key=lambda a: a.page)
    label = f"*{LABELS.get(n.provenance, 'You')}*" + (f" · p. {here[0].page}" if here else "")
    parts = [f"### {_heading(n.body)}", label]
    for a in here:
        quote = "\n".join(f"> {line}".rstrip() if line else ">" for line in a.quoted_text.splitlines())
        chunk = f"&chunk={a.chunk_id}" if a.chunk_id else ""
        parts += [quote, f"[Open in PaperLab](paperlab://papers/{paper_id}?page={a.page}{chunk})"]
    if others := [f"[[{keys[str(p)]}]]" for p in n.paper_ids if p != paper_id and str(p) in keys]:
        parts.append("Also on: " + ", ".join(others))
    parts.append(n.body)
    return "\n\n".join(parts)


def render_paper(paper, key: str, notes: list[NoteItem], keys: dict[str, str], origin: str) -> str:
    """One paper's file: frontmatter, then its notes oldest first (the caller orders them). `keys` maps every
    workspace paper id to its key, for "Also on" links."""
    blocks = [_block(paper.id, n, keys) for n in notes] or ["No notes yet."]
    return f"{_frontmatter(paper, origin)}\n# {neutralise(paper.title)}\n\n" + "\n\n".join(blocks) + "\n"


def render_index(name: str, question: str | None, entries: list[tuple[str, str]]) -> str:
    head = f"# {neutralise(name)}\n\n" + (f"Research question: {question}\n\n" if question else "")
    return head + "\n".join(f"- [[{key}]] {neutralise(title)}" for key, title in entries) + "\n"


async def _notes_by_paper(session: AsyncSession, paper_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[NoteItem]]:
    rows = list(
        await session.scalars(
            select(Note)
            .where(Note.id.in_(select(note_papers.c.note_id).where(note_papers.c.paper_id.in_(paper_ids))))
            .order_by(Note.created_at, Note.id)
        )
    )
    ids = [n.id for n in rows]
    links: dict[uuid.UUID, list[uuid.UUID]] = {}
    for r in await session.execute(select(note_papers).where(note_papers.c.note_id.in_(ids))):
        links.setdefault(r.note_id, []).append(r.paper_id)
    anchors: dict[uuid.UUID, list[AnchorItem]] = {}
    for r in await session.execute(select(note_anchors).where(note_anchors.c.note_id.in_(ids))):
        anchors.setdefault(r.note_id, []).append(AnchorItem(r.paper_id, r.page, r.chunk_id, r.quoted_text or ""))
    by_paper: dict[uuid.UUID, list[NoteItem]] = {}
    for n in rows:
        item = NoteItem(n.body, n.provenance, sorted(links.get(n.id, []), key=str), anchors.get(n.id, []))
        for paper_id in item.paper_ids:
            by_paper.setdefault(paper_id, []).append(item)
    return by_paper


async def export_workspace(session: AsyncSession, workspace_id: uuid.UUID, origin: str) -> bytes:
    """The workspace's notes as a zip: `<slug>/index.md` and `<slug>/<key>.md` per paper. Raises NotFound."""
    papers = await workspaces.papers(session, workspace_id)
    workspace = await session.get(Workspace, workspace_id)
    keys = bibtex_keys([csl_item(p) for p in papers])
    by_paper = await _notes_by_paper(session, [p.id for p in papers])
    slug = filename_slug(workspace.name)
    entries = sorted((keys[str(p.id)], p.title) for p in papers)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/index.md", render_index(workspace.name, workspace.research_question, entries))
        for p in papers:
            text = render_paper(p, keys[str(p.id)], by_paper.get(p.id, []), keys, origin)
            zf.writestr(f"{slug}/{keys[str(p.id)]}.md", text)
    return buffer.getvalue()


MAX_FILES = 50
MAX_BYTES = 1024 * 1024
DOI = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class ImportResult:
    created: int
    linked_papers: int  # created notes linked to at least one library paper
    unmatched_files: int  # created notes with no paper found
    skipped_duplicates: int


def _split(text: str) -> tuple[str, str]:
    """(frontmatter text, body) of a markdown file; the frontmatter is empty when there is none."""
    if m := FRONTMATTER.match(text):
        return m.group(1), text[m.end() :].strip()
    return "", text.strip()


def _dois(text: str) -> set[str]:
    return {m.group().rstrip(".,;)]>").lower() for m in DOI.finditer(text)}


def _check(files: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    if len(files) > MAX_FILES:
        raise InvalidInput(f"Import at most {MAX_FILES} files at a time.")
    texts = []
    for name, data in files:
        if not name.lower().endswith(".md"):
            raise InvalidInput(f"{name} is not a Markdown (.md) file.")
        if len(data) > MAX_BYTES:
            raise InvalidInput(f"{name} is larger than 1 MB.")
        try:
            texts.append((name, data.decode("utf-8")))
        except UnicodeDecodeError:
            raise InvalidInput(f"{name} is not UTF-8 text.") from None
    return texts


async def import_files(session: AsyncSession, files: list[tuple[str, bytes]]) -> ImportResult:
    """Each .md file becomes one unanchored human note, linked to the library papers whose DOI the file mentions.
    A body that already exists as a note is skipped. Raises InvalidInput: limits, encoding, empty file."""
    parsed = []
    for name, text in _check(files):
        front, body = _split(text)
        if not body:
            raise InvalidInput(f"{name} has no text to import.")
        parsed.append((front, body))
    created = linked = skipped = 0
    for front, body in parsed:
        if await session.scalar(select(Note.id).where(Note.body == body).limit(1)):
            skipped += 1
            continue
        dois = _dois(front) | _dois(body)
        found = list(await session.scalars(select(Paper.id).where(func.lower(Paper.doi).in_(dois)))) if dois else []
        note = Note(body=body, provenance=Provenance.HUMAN)
        session.add(note)
        await session.flush()
        await notes._link(session, note.id, found)
        created += 1
        linked += bool(found)
    await session.commit()
    return ImportResult(created, linked, created - linked, skipped)
