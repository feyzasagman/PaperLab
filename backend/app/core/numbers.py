"""Reading numbers the way papers print them. The one parser: cells and captured numbers both go through here.

Handles "88.5", "−3", "1,234", "88.5 ± 0.3", "34%", "110M", "14k", "1.2e-3", and best-result or footnote marks ("90.9*",
"90.9†", "**90.9**"). A cell that isn't exactly one of those ("BERT-L", "—", "88.5 F1") has no value: it's a label.
ponytail: "." is the only decimal separator ("1,5" is not 1.5), and "1.2×10^-3" isn't read; add them when a paper
needs them.
"""

import re
from dataclasses import dataclass

_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+"
# A scale suffix must touch its number: "14K" is 14 thousand, but "310 K" is 310 kelvin, and reading that as
# 310,000 put a silently wrong number in a chart. Whitespace stays allowed before "%", which is never a unit.
_AMOUNT = rf"[+-]?(?:{_NUMBER})(?:[KMBkmb])?(?:\s*%)?"
_WITH_ERROR = re.compile(rf"(?P<value>{_AMOUNT})(?:\s*±\s*(?P<error>{_AMOUNT}))?")
_PARTS = re.compile(rf"(?P<sign>[+-]?)(?P<number>{_NUMBER})(?P<suffix>[KMBkmb]?)")
_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}
_MARKS = "*†‡§¶"
_NOT_UNITS = {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "vs", "with"}
_UNIT = re.compile(r"\s*([A-Za-z][\w@-]{0,15})")


@dataclass(frozen=True)
class ParsedNumber:
    value: float | None
    error: float | None


@dataclass(frozen=True)
class NumberCandidate:
    raw: str
    value: float
    error: float | None
    unit_hint: str | None


def _clean(raw: str) -> str:
    text = raw.replace("−", "-").strip()
    if text.startswith("**") and text.endswith("**"):
        text = text[2:-2].strip()
    return text.rstrip(_MARKS).strip()


def _amount(text: str) -> float:
    parts = _PARTS.match(text)
    assert parts is not None  # only called on text _AMOUNT matched
    number = float(parts["number"].replace(",", "")) * _SCALE[parts["suffix"].upper()]
    return -number if parts["sign"] == "-" else number


def parse_number(raw: str) -> ParsedNumber:
    match = _WITH_ERROR.fullmatch(_clean(raw))
    if match is None:
        return ParsedNumber(value=None, error=None)
    error = match["error"]
    return ParsedNumber(value=_amount(match["value"]), error=None if error is None else _amount(error))


def find_numbers(text: str) -> list[NumberCandidate]:
    """Every number in a selection, in order, for the owner to pick from. Digits inside words ("GPT-2", "v2",
    "3x") are skipped. unit_hint is "%" or the word right after the number, unless that word is a common short word."""
    text = text.replace("−", "-")
    found = []
    for match in _WITH_ERROR.finditer(text):
        before = text[match.start() - 1] if match.start() > 0 else " "
        after = text[match.end()] if match.end() < len(text) else " "
        starts_unsigned = match.group(0)[0] not in "+-"
        if before.isalnum() or before in "._" or (starts_unsigned and before in "+-") or after.isalnum():
            continue
        raw = match.group(0).strip()
        if raw.endswith("%"):
            unit = "%"
        else:
            word = _UNIT.match(text, match.end())
            unit = word[1] if word and word[1].lower() not in _NOT_UNITS else None
        parsed = parse_number(raw)
        found.append(NumberCandidate(raw=raw, value=parsed.value, error=parsed.error, unit_hint=unit))
    return found


# Matching numbers across a quote and a card value (M39 F5, D242): the same figure typeset two ways compares equal.
_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-"})  # parse_number's "−" is below
_DIGIT_SPACES = re.compile(r"(?<=\d)[\u2009\u202f\u00a0\u200a](?=\d)")  # thin / no-break space inside 12 345
# A sign counts only when it isn't glued to a word or number (so "2019-2020" is two years, not 2019 and -2020).
# "1,234" is one number; "0.21, 0.69" stays two because a thousands group is exactly three digits with no space.
_TOKEN = re.compile(r"(?:(?<![\w.])([-+]))?((?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?|\.\d+)")


def tokens(text: str) -> list[str]:
    cleaned = _DIGIT_SPACES.sub("", text.replace("\u2212", "-").translate(_DASHES))
    out = []
    for sign, digits in _TOKEN.findall(cleaned):
        digits = digits.replace(",", "")
        out.append(f"{sign}{'0' if digits.startswith('.') else ''}{digits}")
    return out


def all_in(value: str, source: str) -> bool:
    """Every number in `value` appears in `source` (a value with no numbers passes)."""
    return set(tokens(value)) <= set(tokens(source))
