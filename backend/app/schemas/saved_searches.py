import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

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

    # Only year_from/year_to (and the schedule parts) may be sent as null; these four would break the update.
    @field_validator("query", "sources", "timezone", "enabled")
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise ValueError("cannot be null")
        return value


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
