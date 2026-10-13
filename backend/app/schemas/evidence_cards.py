import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    paper_id: uuid.UUID
    status: str
    error: str | None
    fields: dict
    provenance: str
    model: str | None
    prompt_version: int | None
    updated_at: datetime | None


class CardRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    paper_id: uuid.UUID
    title: str
    year: int | None
    first_author: str | None
    card: CardOut | None


class GenerateIn(BaseModel):
    regenerate: bool = False
    confirm: bool = False


class EditIn(BaseModel):
    changes: list[dict]  # {field, index?, values: {key: value}}
