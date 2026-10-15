import re
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.core.candidates import MAX_AUTHORS, MAX_PDF_URLS, Candidate
from app.schemas.paper_sources import SourceId

AuthorName = Annotated[str, Field(min_length=1, max_length=300)]

# One generic dict field instead of one field per source, so a new source needs one new entry here, not a new
# CandidateIn/CandidateOut field. The openalex pattern caps the total length at 20 characters, as the old field did.
SOURCE_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "dblp": re.compile(r"^[a-z]+(/[A-Za-z0-9_-]+)+$"),
    "arxiv": re.compile(r"^(\d{4}\.\d{4,5}|[A-Za-z-]+(\.[A-Za-z]{2})?/\d{7})$"),
    "openalex": re.compile(r"^W\d{1,19}$"),
    "semantic_scholar": re.compile(r"^[0-9a-f]{40}$"),
    "core": re.compile(r"^\d{1,20}$"),
    "pubmed": re.compile(r"^\d{1,20}$"),
    "pmc": re.compile(r"^\d{1,20}$"),
    "zenodo": re.compile(r"^\d{1,20}$"),
    "hal": re.compile(r"^\d{1,20}$"),
    "doaj": re.compile(r"^[0-9a-f]{32}$"),
    "openaire": re.compile(r"^[0-9a-z_]{1,20}::[0-9a-f]{32}$"),
    "iacr_eprint": re.compile(r"^\d{4}/\d{1,6}$"),
    "biorxiv": re.compile(r"^10\.\d{4,9}/\S+$"),
    "medrxiv": re.compile(r"^10\.\d{4,9}/\S+$"),
}


class CandidateOut(BaseModel):
    """A paper found outside the library. `sources` found it, most trusted first; `paper_id` is set when the library
    already holds it."""

    model_config = ConfigDict(from_attributes=True)

    title: str
    authors: list[str]
    year: int | None
    venue: str | None
    doi: str | None
    external_ids: dict[str, str]
    cited_by_count: int | None
    pdf_urls: list[str]
    sources: list[SourceId]
    paper_id: uuid.UUID | None


class SearchOut(BaseModel):
    """`notices` name the sources that failed, whose results are missing."""

    model_config = ConfigDict(from_attributes=True)

    results: list[CandidateOut]
    notices: list[str]


class CandidateIn(BaseModel):
    """A search result or suggestion sent back to be added. Extra fields (its `paper_id`, its `sources`) are ignored:
    the library is checked again."""

    title: str = Field(min_length=1, max_length=1000)
    authors: list[AuthorName] = Field(default_factory=list, max_length=MAX_AUTHORS)
    year: int | None = Field(default=None, ge=1000, le=2100)
    venue: str | None = Field(default=None, max_length=1000)
    doi: str | None = Field(default=None, pattern=r"^10\.\d{4,9}/\S+$", max_length=300)
    external_ids: dict[str, str] = Field(default_factory=dict)
    cited_by_count: int | None = Field(default=None, ge=0)
    pdf_urls: list[HttpUrl] = Field(default_factory=list, max_length=MAX_PDF_URLS)

    @model_validator(mode="after")
    def _external_ids_are_known_and_well_formed(self) -> "CandidateIn":
        for source, value in self.external_ids.items():
            pattern = SOURCE_ID_PATTERNS.get(source)
            if pattern is None or not pattern.fullmatch(value):
                raise ValueError(f"'{value}' is not a valid {source} id")
        return self

    def to_candidate(self) -> Candidate:
        return Candidate(**self.model_dump(exclude={"pdf_urls"}), pdf_urls=[str(url) for url in self.pdf_urls])


class AddPaperRequest(BaseModel):
    candidate: CandidateIn
    # Also file the new paper in this workspace (404 before any download when it's unknown).
    workspace_id: uuid.UUID | None = None
