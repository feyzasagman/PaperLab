import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WorkspaceCreate(BaseModel):
    name: str  # stripped and checked (1..80 characters) by core/workspaces.py, which answers 422


class WorkspaceRename(BaseModel):
    name: str


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    paper_count: int
    note_count: int
    created_at: datetime
    description: str | None = None
