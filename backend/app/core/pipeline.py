"""What Claude does over MCP (M39 F1): each function backs one tool, composing existing core services.

Workspaces come by name (D93). Jobs go through `queue`, paper sources through `discovery.open_providers`. No SQL here:
every read and write is an existing service's. Results are the API's own schemas, so Claude and the app see the same
fields.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import papers, workspaces
from app.schemas.papers import PaperOut

workspace_id = workspaces.by_name


@dataclass(frozen=True)
class Removed:
    workspace: str
    paper_id: uuid.UUID
    removed: bool = True


async def list_papers(session: AsyncSession, workspace: str) -> list[PaperOut]:
    rows = await workspaces.papers(session, await workspace_id(session, workspace))
    return [PaperOut.model_validate(row) for row in rows]


async def add_paper_to_workspace(session: AsyncSession, workspace: str, paper_id: uuid.UUID) -> PaperOut:
    await workspaces.add_paper(session, await workspace_id(session, workspace), paper_id)
    return PaperOut.model_validate(await papers.get_paper(session, paper_id))


async def remove_paper_from_workspace(session: AsyncSession, workspace: str, paper_id: uuid.UUID) -> Removed:
    await workspaces.remove_paper(session, await workspace_id(session, workspace), paper_id)
    return Removed(workspace=workspace.strip(), paper_id=paper_id)
