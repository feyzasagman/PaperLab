"""A search run's year range (M39 F2, D221)."""

import pytest

from app.core import year_range
from app.core.errors import InvalidInput


@pytest.mark.parametrize("filters", [{}, {"year_from": None, "year_to": None}])
def test_no_range_is_none(filters):
    assert year_range.parse(filters) is None


def test_one_ended_range_keeps_only_its_key():
    years = year_range.parse({"year_from": 2023})
    assert years.as_filters() == {"year_from": 2023}
    assert years.keeps(2023) and years.keeps(2030) and not years.keeps(2022)


def test_an_undated_record_is_kept():
    assert year_range.parse({"year_from": 2023, "year_to": 2026}).keeps(None)


@pytest.mark.parametrize(
    "filters",
    [{"year_from": 2026, "year_to": 2023}, {"year_from": "2023"}, {"year_from": True}, {"year_to": 1800},
     {"since": 2020}],
)
def test_bad_ranges_are_invalid_years(filters):
    with pytest.raises(InvalidInput) as raised:
        year_range.parse(filters)
    assert str(raised.value) == "invalid_years"
