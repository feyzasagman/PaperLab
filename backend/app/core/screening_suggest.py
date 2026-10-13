"""The local-model screening suggestion for one hit (M31b spec §4.4): the prompt, and a parser that turns anything
off-format into `unsure` so a malformed reply never becomes an exclude."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.core import llm_json, notes, prompts

SCREENING_PROMPT_VERSION = 2
SCREENING_PROMPT_VERSION_V1 = 1
SYSTEM_PROMPT, PROMPT_TEMPLATE = prompts.load("screening_suggest", SCREENING_PROMPT_VERSION_V1).split(
    "\n<!-- prompt -->\n"
)
SYSTEM_PROMPT_V2, PROMPT_TEMPLATE_V2 = prompts.load("screening_suggest", SCREENING_PROMPT_VERSION).split(
    "\n<!-- prompt -->\n"
)
MAX_QUOTES = 3
EXCLUDE_REASONS = ("wrong_topic", "wrong_study_type", "duplicate", "language", "inaccessible", "other")
NOTE_CHARS = 300


@dataclass(frozen=True)
class Suggestion:
    verdict: str  # include | exclude | unsure
    reason: str | None
    note: str | None


def build_prompt(criteria: str, title: str | None, abstract: str | None) -> str:
    # str.format only parses the template, so braces in the criteria or abstract are safe.
    return PROMPT_TEMPLATE.format(criteria=criteria, title=title or "(no title)", abstract=abstract or "(no abstract)")


def parse_reply(text: str) -> Suggestion:
    """The model's two-line reply. Anything that isn't exactly the format is `unsure`: a malformed reply never
    becomes an exclude and never raises."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return Suggestion("unsure", None, None)
    note = lines[1][:NOTE_CHARS] if len(lines) > 1 else None
    words = lines[0].lower().split()
    if words == ["include"]:
        return Suggestion("include", None, note)
    if len(words) == 2 and words[0] == "exclude" and words[1] in EXCLUDE_REASONS:
        return Suggestion("exclude", words[1], note)
    return Suggestion("unsure", None, note if words == ["unsure"] else None)


class _Reply(BaseModel):
    relevance: int = Field(ge=1, le=5)
    decision: Literal["include", "exclude", "unsure"]
    reason: Literal[*EXCLUDE_REASONS] | None = None
    quotes: list[str] = Field(default_factory=list, max_length=10)
    why: str = ""


@dataclass(frozen=True)
class SuggestionV2:
    verdict: str
    reason: str | None
    note: str | None
    relevance: int | None = None
    quotes: list[str] = field(default_factory=list)
    confidence: str | None = None  # high | low
    flags: list[str] = field(default_factory=list)


def build_prompt_v2(question, inclusion, exclusion, title, abstract) -> str:
    return PROMPT_TEMPLATE_V2.format(
        question=question or "(none given)", inclusion=inclusion or "(none given)",
        exclusion=exclusion or "(none given)", title=title or "(no title)", abstract=abstract,
    )


async def suggest(llm, question, inclusion, exclusion, title, abstract) -> SuggestionV2:
    """One hit's suggestion. No abstract -> unsure + no_abstract without a model call. Quotes are kept only when they
    are in the abstract (notes.fold); any drop, or an include/exclude with no kept quote, makes it low confidence."""
    if not (abstract or "").strip():
        return SuggestionV2("unsure", None, None, flags=["no_abstract"])
    reply = await llm_json.ask_json(
        llm, SYSTEM_PROMPT_V2, build_prompt_v2(question, inclusion, exclusion, title, abstract), _Reply
    )
    if reply is None:
        return SuggestionV2("unsure", None, "model error", flags=["model_error"])
    folded = notes.fold(abstract)
    asked = [q.strip() for q in reply.quotes if q.strip()][:MAX_QUOTES]
    kept = [q for q in asked if notes.fold(q) in folded]
    flags = ["quote_dropped"] if len(kept) < len(asked) else []
    low = bool(flags) or (reply.decision != "unsure" and not kept)
    return SuggestionV2(
        verdict=reply.decision, reason=reply.reason if reply.decision == "exclude" else None,
        note=reply.why.strip()[:NOTE_CHARS] or None, relevance=reply.relevance, quotes=kept,
        confidence="low" if low else "high", flags=flags,
    )
