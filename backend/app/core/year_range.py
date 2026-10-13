"""A search run's year range (M39 F2, D221): validated once in start_run, applied to every source's results in
search_batch. A record with no year is kept — it can't be excluded honestly."""

from dataclasses import dataclass

from app.core.errors import InvalidInput

MIN_YEAR, MAX_YEAR = 1900, 2100
KEYS = ("year_from", "year_to")


@dataclass(frozen=True)
class YearRange:
    year_from: int | None = None
    year_to: int | None = None

    def keeps(self, year: int | None) -> bool:
        if year is None:
            return True
        return (self.year_from is None or year >= self.year_from) and (self.year_to is None or year <= self.year_to)

    def as_filters(self) -> dict:
        return {key: value for key in KEYS if (value := getattr(self, key)) is not None}


def _year(value) -> bool:
    return value is None or (type(value) is int and MIN_YEAR <= value <= MAX_YEAR)


def parse(filters: dict) -> YearRange | None:
    """None when no year is given. Raises InvalidInput("invalid_years") for another key, a non-integer, a year outside
    1900–2100, or year_from after year_to."""
    if set(filters) - set(KEYS) or not all(_year(filters.get(key)) for key in KEYS):
        raise InvalidInput("invalid_years", keys=list(KEYS), min=MIN_YEAR, max=MAX_YEAR)
    years = YearRange(filters.get("year_from"), filters.get("year_to"))
    if years.year_from is not None and years.year_to is not None and years.year_from > years.year_to:
        raise InvalidInput("invalid_years", hint="year_from is after year_to")
    return years if years.as_filters() else None
