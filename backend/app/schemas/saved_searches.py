import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.workspace_search import SearchSource

ScheduleKind = Literal["daily", "weekly", "custom"]


class SavedSearchCreate(BaseModel):
    query: str
    sources: list[SearchSource] = []
    year_from: int | None = None
    year_to: int | None = None
    schedule_kind: ScheduleKind
    time: str | None = None
    weekday: int | None = None
    cron: str | None = None
    timezone: str


class SavedSearchUpdate(BaseModel):
    query: str | None = None
    sources: list[SearchSource] | None = None
    year_from: int | None = None
    year_to: int | None = None
    schedule_kind: ScheduleKind | None = None
    time: str | None = None
    weekday: int | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


class SavedSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    query: str
    sources: list[str]
    year_from: int | None
    year_to: int | None
    schedule: str
    timezone: str
    schedule_kind: str
    time: str | None
    weekday: int | None
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_run_id: uuid.UUID | None
    last_run_status: str | None
    last_error: str | None
    created_at: datetime
