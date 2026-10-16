"""Workspace tags: names that group a workspace's papers, e.g. by research question.

A tag belongs to one workspace, matches case-insensitively and keeps the name as first written. Only members of the
workspace can be tagged, and a paper leaving the workspace loses its tags there (the paper_tags FK cascades).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy import delete as delete_rows
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enrichment import normalize_doi
from app.core.errors import Conflict, InvalidInput
from app.models import Paper, WorkspaceTag, hit_tags, paper_tags, workspace_papers
from app.models.workspace_search import WorkspaceSearchHit

NAME_MAX_CHARS = 64
MAX_TAG_BATCH = 5000


@dataclass(frozen=True)
class TagResult:
    tag: str
    changed: int
    unchanged: int
    skipped: list[str]


@dataclass(frozen=True)
class TagSummary:
    name: str
    papers: int
    peer_reviewed: int
    preprint: int
    unknown_review: int
    full_text: int
    abstract: int
    metadata: int


def normalize(name: str) -> str:
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= NAME_MAX_CHARS:
        raise InvalidInput("invalid_tag_name", max=NAME_MAX_CHARS)
    return cleaned


async def _all(session: AsyncSession, ws: uuid.UUID) -> list[WorkspaceTag]:
    query = select(WorkspaceTag).where(WorkspaceTag.workspace_id == ws).order_by(func.lower(WorkspaceTag.name))
    return list(await session.scalars(query))


async def _find(session: AsyncSession, ws: uuid.UUID, name: str) -> WorkspaceTag | None:
    query = select(WorkspaceTag).where(WorkspaceTag.workspace_id == ws, func.lower(WorkspaceTag.name) == name.lower())
    return await session.scalar(query)


async def get_or_create(session: AsyncSession, ws: uuid.UUID, name: str) -> WorkspaceTag:
    name = normalize(name)
    tag = await _find(session, ws, name)
    if tag is None:
        # ponytail: check-then-insert; a concurrent create of the same name hits the unique index. Single user.
        tag = WorkspaceTag(workspace_id=ws, name=name)
        session.add(tag)
        await session.commit()
    return tag


async def resolve(session: AsyncSession, ws: uuid.UUID, names: list[str]) -> list[WorkspaceTag]:
    """One tag per name, in order. Raises InvalidInput("unknown_tag", tag=..., available=[every tag name])."""
    known = await _all(session, ws)
    by_lower = {t.name.lower(): t for t in known}
    out = []
    for name in names:
        tag = by_lower.get(normalize(name).lower())
        if tag is None:
            raise InvalidInput("unknown_tag", tag=name, available=[t.name for t in known])
        out.append(tag)
    return out


async def _members(
    session: AsyncSession, ws: uuid.UUID, paper_ids: Sequence[uuid.UUID], dois: Sequence[str]
) -> tuple[set[uuid.UUID], list[str]]:
    """The workspace members among the ids and DOIs, and what matched none of them (as given)."""
    if not paper_ids and not dois:
        raise InvalidInput("no_papers_given")
    if len(paper_ids) + len(dois) > MAX_TAG_BATCH:
        raise InvalidInput("too_many_papers", max=MAX_TAG_BATCH)
    wanted = {d: normalize_doi(d) or d.strip().lower() for d in dois}
    query = (
        select(Paper.id, func.lower(Paper.doi))
        .join(workspace_papers, workspace_papers.c.paper_id == Paper.id)
        .where(
            workspace_papers.c.workspace_id == ws,
            or_(Paper.id.in_(paper_ids), func.lower(Paper.doi).in_(set(wanted.values()))),
        )
    )
    rows = (await session.execute(query)).all()
    found_ids = {r[0] for r in rows}
    found_dois = {r[1] for r in rows if r[1]}
    skipped = [str(i) for i in paper_ids if i not in found_ids] + [d for d, w in wanted.items() if w not in found_dois]
    return found_ids, skipped


async def tag_papers(
    session: AsyncSession,
    ws: uuid.UUID,
    name: str,
    paper_ids: Sequence[uuid.UUID] = (),
    dois: Sequence[str] = (),
) -> TagResult:
    """Tags the workspace's papers, creating the tag when missing. Non-members are skipped, never added."""
    name = normalize(name)
    members, skipped = await _members(session, ws, paper_ids, dois)
    tag = await get_or_create(session, ws, name)
    rows = [{"workspace_id": ws, "paper_id": pid, "tag_id": tag.id} for pid in members]
    inserted = 0
    if rows:
        done = await session.execute(
            insert(paper_tags).values(rows).on_conflict_do_nothing().returning(paper_tags.c.paper_id)
        )
        inserted = len(done.all())
    await session.commit()
    return TagResult(tag.name, inserted, len(members) - inserted, skipped)


async def untag_papers(
    session: AsyncSession,
    ws: uuid.UUID,
    name: str,
    paper_ids: Sequence[uuid.UUID] = (),
    dois: Sequence[str] = (),
) -> TagResult:
    """Raises InvalidInput("unknown_tag") rather than creating the tag."""
    (tag,) = await resolve(session, ws, [name])
    members, skipped = await _members(session, ws, paper_ids, dois)
    done = await session.execute(
        delete_rows(paper_tags)
        .where(paper_tags.c.tag_id == tag.id, paper_tags.c.paper_id.in_(members))
        .returning(paper_tags.c.paper_id)
    )
    removed = len(done.all())
    await session.commit()
    return TagResult(tag.name, removed, len(members) - removed, skipped)


async def rename(session: AsyncSession, ws: uuid.UUID, name: str, new_name: str) -> str:
    (tag,) = await resolve(session, ws, [name])
    new_name = normalize(new_name)
    other = await _find(session, ws, new_name)
    if other is not None and other.id != tag.id:
        raise Conflict("tag_exists", tag=new_name)
    tag.name = new_name
    await session.commit()
    return new_name


async def delete(session: AsyncSession, ws: uuid.UUID, name: str) -> int:
    """Removes the tag; returns how many papers carried it. The papers are untouched."""
    (tag,) = await resolve(session, ws, [name])
    carried = await session.scalar(select(func.count()).select_from(paper_tags).where(paper_tags.c.tag_id == tag.id))
    await session.execute(delete_rows(WorkspaceTag).where(WorkspaceTag.id == tag.id))
    await session.commit()
    return carried


async def summaries(session: AsyncSession, ws: uuid.UUID) -> list[TagSummary]:
    """Every tag of the workspace by name, with how its papers split by review status and evidence access."""
    full_text = and_(Paper.status == "ready", Paper.has_chunks)  # as Paper.evidence_access
    has_abstract = and_(~full_text, func.coalesce(Paper.abstract, "") != "")
    peer = Paper.peer_reviewed == "true"
    preprint = and_(~peer, Paper.source_type.is_not_distinct_from("preprint"))

    def count(cond):
        return func.count(Paper.id).filter(cond)

    query = (
        select(
            WorkspaceTag.name,
            func.count(Paper.id),
            count(peer),
            count(preprint),
            count(~peer & ~preprint),
            count(full_text),
            count(has_abstract),
            count(~full_text & ~has_abstract),
        )
        .select_from(WorkspaceTag)
        .join(paper_tags, paper_tags.c.tag_id == WorkspaceTag.id, isouter=True)
        .join(Paper, Paper.id == paper_tags.c.paper_id, isouter=True)
        .where(WorkspaceTag.workspace_id == ws)
        .group_by(WorkspaceTag.id, WorkspaceTag.name)
        .order_by(func.lower(WorkspaceTag.name))
    )
    return [TagSummary(*row) for row in await session.execute(query)]


async def paper_ids(
    session: AsyncSession, ws: uuid.UUID, names: list[str] | None, match: Literal["any", "all"] = "any"
) -> set[uuid.UUID] | None:
    """The workspace papers carrying any/all of the tags; None (no filter) when no tags are given."""
    if not names:
        return None
    if match not in ("any", "all"):
        raise InvalidInput("invalid_match", valid=["any", "all"])
    tag_ids = {t.id for t in await resolve(session, ws, names)}
    query = select(paper_tags.c.paper_id).where(paper_tags.c.workspace_id == ws, paper_tags.c.tag_id.in_(tag_ids))
    if match == "all":
        query = query.group_by(paper_tags.c.paper_id).having(func.count() == len(tag_ids))
    else:
        query = query.distinct()
    return set(await session.scalars(query))


async def apply_hit_tags(session: AsyncSession, ws: uuid.UUID, hits: Sequence[WorkspaceSearchHit]) -> None:
    """Copies the hits' tags onto their papers, for hits whose paper is a member of the workspace. No commit."""
    ids = [h.id for h in hits if h.paper_id is not None]
    if not ids:
        return
    wanted = (
        select(WorkspaceSearchHit.workspace_id, WorkspaceSearchHit.paper_id, hit_tags.c.tag_id)
        .join(hit_tags, hit_tags.c.hit_id == WorkspaceSearchHit.id)
        .join(
            workspace_papers,
            (workspace_papers.c.workspace_id == WorkspaceSearchHit.workspace_id)
            & (workspace_papers.c.paper_id == WorkspaceSearchHit.paper_id),
        )
        .where(WorkspaceSearchHit.id.in_(ids), WorkspaceSearchHit.workspace_id == ws)
    )
    await session.flush()  # an INSERT..SELECT doesn't autoflush the hits' fresh paper_id
    await session.execute(
        insert(paper_tags).from_select(["workspace_id", "paper_id", "tag_id"], wanted).on_conflict_do_nothing()
    )


async def tag_hits(session: AsyncSession, ws: uuid.UUID, names: list[str], hit_ids: Sequence[uuid.UUID]) -> None:
    """Stores the tags on the workspace's hits (creating missing tags); a hit already imported tags its paper too."""
    hits = list(
        await session.scalars(
            select(WorkspaceSearchHit).where(WorkspaceSearchHit.workspace_id == ws, WorkspaceSearchHit.id.in_(hit_ids))
        )
    )
    if not names or not hits:
        return
    tag_ids = {(await get_or_create(session, ws, name)).id for name in names}
    rows = [{"hit_id": h.id, "tag_id": t} for h in hits for t in tag_ids]
    await session.execute(insert(hit_tags).values(rows).on_conflict_do_nothing())
    await apply_hit_tags(session, ws, hits)
    await session.commit()
