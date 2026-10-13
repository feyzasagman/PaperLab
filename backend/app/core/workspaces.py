"""Workspaces: named, flat sets of shared papers. A workspace's notes are the notes linked to its papers.

Removing a paper from a workspace, or deleting a workspace, never deletes papers or notes.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete as delete_rows
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, InvalidInput, NotFound
from app.core.notes import NoteView, _with_anchors, reading_position
from app.core.papers import get_paper
from app.models import Note, Paper, Workspace, note_papers, workspace_papers

NAME_MAX_CHARS = 80


@dataclass(frozen=True)
class WorkspaceView:
    id: uuid.UUID
    name: str
    paper_count: int
    note_count: int
    created_at: datetime
    description: str | None = None


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= NAME_MAX_CHARS:
        raise InvalidInput(f"a workspace name needs 1 to {NAME_MAX_CHARS} characters")
    return cleaned


async def _ensure_name_free(session: AsyncSession, name: str, workspace_id: uuid.UUID | None = None) -> None:
    # ponytail: check-then-write; two concurrent creates would hit UNIQUE (name) as a 500. Single user.
    taken = select(Workspace.id).where(Workspace.name == name, Workspace.id != workspace_id)
    if await session.scalar(taken) is not None:
        raise Conflict("workspace_name_taken")


async def _views(session: AsyncSession, *where) -> list[WorkspaceView]:
    member = workspace_papers.c.workspace_id == Workspace.id
    paper_count = select(func.count()).select_from(workspace_papers).where(member)
    papers_notes = note_papers.join(workspace_papers, workspace_papers.c.paper_id == note_papers.c.paper_id)
    # DISTINCT: a note linked to two of the workspace's papers is one note.
    note_count = select(func.count(func.distinct(note_papers.c.note_id))).select_from(papers_notes).where(member)
    query = select(
        Workspace.id,
        Workspace.name,
        paper_count.scalar_subquery().label("paper_count"),
        note_count.scalar_subquery().label("note_count"),
        Workspace.created_at,
        Workspace.description,
    )
    rows = await session.execute(query.where(*where).order_by(Workspace.name))
    return [WorkspaceView(*row) for row in rows]


async def get(session: AsyncSession, workspace_id: uuid.UUID) -> WorkspaceView:
    views = await _views(session, Workspace.id == workspace_id)
    if not views:
        raise NotFound(f"workspace {workspace_id} not found")
    return views[0]


async def list_workspaces(session: AsyncSession) -> list[WorkspaceView]:
    """Alphabetical, as the sidebar lists them."""
    return await _views(session)


async def by_name(session: AsyncSession, name: str) -> uuid.UUID:
    """The id of the workspace with exactly this name (trimmed), for callers that know workspaces by name (MCP).

    Raises NotFound("unknown_workspace", available=[every name, alphabetical]).
    """
    views = await list_workspaces(session)
    match = next((view.id for view in views if view.name == name.strip()), None)
    if match is None:
        raise NotFound("unknown_workspace", available=[view.name for view in views])
    return match


async def create(session: AsyncSession, name: str, description: str | None = None) -> WorkspaceView:
    """Raises InvalidInput (blank or over 80 characters) or Conflict("workspace_name_taken")."""
    name = _clean_name(name)
    await _ensure_name_free(session, name)
    workspace = Workspace(name=name, description=(description or "").strip() or None)
    session.add(workspace)
    await session.commit()
    return await get(session, workspace.id)


async def rename(session: AsyncSession, workspace_id: uuid.UUID, name: str) -> WorkspaceView:
    await get(session, workspace_id)
    name = _clean_name(name)
    await _ensure_name_free(session, name, workspace_id)
    workspace = await session.get(Workspace, workspace_id)
    workspace.name = name
    await session.commit()
    return await get(session, workspace_id)


async def delete(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Memberships and the workspace's chat answers cascade; papers and notes stay."""
    await get(session, workspace_id)
    await session.execute(delete_rows(Workspace).where(Workspace.id == workspace_id))
    await session.commit()


async def _membership(session: AsyncSession, workspace_id: uuid.UUID, paper_id: uuid.UUID) -> Paper:
    await get(session, workspace_id)
    return await get_paper(session, paper_id)


async def add_paper(session: AsyncSession, workspace_id: uuid.UUID, paper_id: uuid.UUID) -> None:
    """Idempotent. Raises NotFound for an unknown workspace or paper."""
    paper = await _membership(session, workspace_id, paper_id)
    await session.execute(
        insert(workspace_papers).values(workspace_id=workspace_id, paper_id=paper_id).on_conflict_do_nothing()
    )
    await session.commit()
    await session.refresh(paper, ["workspace_ids"])  # the session may already hold this paper


async def remove_paper(session: AsyncSession, workspace_id: uuid.UUID, paper_id: uuid.UUID) -> None:
    """Idempotent: removing a paper that isn't a member is not an error."""
    paper = await _membership(session, workspace_id, paper_id)
    await session.execute(
        delete_rows(workspace_papers).where(
            workspace_papers.c.workspace_id == workspace_id, workspace_papers.c.paper_id == paper_id
        )
    )
    await session.commit()
    await session.refresh(paper, ["workspace_ids"])


async def papers(session: AsyncSession, workspace_id: uuid.UUID) -> list[Paper]:
    """The workspace's papers, newest first like the library."""
    await get(session, workspace_id)
    members = select(workspace_papers.c.paper_id).where(workspace_papers.c.workspace_id == workspace_id)
    query = select(Paper).where(Paper.id.in_(members)).order_by(Paper.created_at.desc())
    return list(await session.scalars(query))


async def notes(session: AsyncSession, workspace_id: uuid.UUID) -> list[NoteView]:
    """Notes linked to the workspace's papers, each once, under its first linked paper there by title (then id). Under
    a paper, the notes with no passage on it come first, newest first; then the rest in reading order."""
    members = {p.id: p for p in await papers(session, workspace_id)}
    linked = select(note_papers.c.note_id).where(note_papers.c.paper_id.in_(members))
    views = await _with_anchors(session, list(await session.scalars(select(Note).where(Note.id.in_(linked)))))

    def position(view: NoteView):
        # paper_ids is sorted by (title, id) already, so the first one in the workspace is the paper it sorts under.
        paper_id = next(pid for pid in view.paper_ids if pid in members)
        under = (members[paper_id].title, str(paper_id))
        if any(anchor.paper_id == paper_id for anchor in view.anchors):
            return (*under, 1, *reading_position(view, paper_id))
        return (*under, 0, -view.created_at.timestamp())

    return sorted(views, key=position)
