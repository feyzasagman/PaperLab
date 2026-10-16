import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.source_types import PeerReviewed, SourceType

# PDF points, top-left origin: (x0, y0, x1, y1). A tuple so OpenAPI (and the TS types) say 4 numbers.
Rect = tuple[float, float, float, float]

Triage = Literal["keep", "later", "drop"]


class PaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doi: str | None
    openalex_id: str | None
    title: str
    abstract: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    type: str | None
    source_type: SourceType | None
    peer_reviewed: PeerReviewed
    published_doi: str | None
    published_venue: str | None
    evidence_access: Literal["full_text", "abstract", "metadata"]
    is_retracted: bool
    oa_status: str | None
    oa_url: str | None
    cited_by_count: int | None
    referenced_works_count: int | None
    issn: str | None
    page_count: int | None
    status: str
    status_error: str | None
    created_at: datetime
    workspace_ids: list[uuid.UUID]
    reading_pass: int
    triage: Triage | None


AuthorName = Annotated[str, Field(min_length=1, max_length=300)]


class PaperUpdate(BaseModel):
    """A manual correction. Only the fields sent change; null clears venue, doi, abstract or year."""

    # D163: the reading state is not a correction; PATCH answers 422 to it instead of dropping it with a 200.
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=1000)
    authors: list[AuthorName] | None = Field(default=None, max_length=500)
    year: int | None = Field(default=None, ge=1000, le=2100)
    venue: str | None = Field(default=None, max_length=1000)
    doi: str | None = Field(default=None, max_length=300)
    # OpenAlex sometimes returns a wrong abstract (BERT's was its citation string); the user can fix or clear it.
    abstract: str | None = Field(default=None, max_length=10_000)
    is_retracted: bool | None = None
    source_type: SourceType | None = None  # null clears it
    peer_reviewed: PeerReviewed | None = None

    @model_validator(mode="after")
    def is_a_correction(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send at least one field to correct")
        required = {"authors", "is_retracted", "title", "peer_reviewed"} & self.model_fields_set
        nulled = sorted(field for field in required if getattr(self, field) is None)
        if nulled:
            raise ValueError(f"{', '.join(nulled)} can't be null")
        return self


class ReadingIn(BaseModel):
    """The reader's own record of a paper (D119). Only the fields sent change; `triage: null` clears the decision."""

    model_config = ConfigDict(extra="forbid")

    reading_pass: int | None = Field(None, ge=0, le=3)
    triage: Triage | None = None

    @model_validator(mode="after")
    def sets_something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send reading_pass, triage or both")
        if "reading_pass" in self.model_fields_set and self.reading_pass is None:
            raise ValueError("reading_pass can't be null")
        return self


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    page: int
    bbox: list[Rect]
    section_title: str | None
    text: str
    strategy_ver: int
