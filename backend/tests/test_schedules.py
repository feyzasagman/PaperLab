"""Saved-search schedules: cron strings and next fire times (M39 F2, D222/D223)."""

from datetime import datetime, timezone

import pytest

from app.core import schedules
from app.core.errors import InvalidInput

UTC = timezone.utc


def test_daily_and_weekly_become_cron_and_read_back():
    assert schedules.to_cron("daily", time="02:00") == "0 2 * * *"
    assert schedules.to_cron("weekly", time="18:30", weekday=1) == "30 18 * * 1"
    assert schedules.describe("0 2 * * *") == ("daily", "02:00", None)
    assert schedules.describe("30 18 * * 1") == ("weekly", "18:30", 1)
    assert schedules.describe("*/15 9-17 * * 1-5") == ("custom", None, None)


@pytest.mark.parametrize("args", [
    {"kind": "daily", "time": "24:00"}, {"kind": "daily", "time": "2:00"}, {"kind": "weekly", "time": "02:00"},
    {"kind": "weekly", "time": "02:00", "weekday": 7}, {"kind": "cron", "cron": "not cron"},
    {"kind": "cron", "cron": "0 2 * * * *"}, {"kind": "hourly"},
])
def test_bad_schedules_are_invalid_schedule(args):
    with pytest.raises(InvalidInput) as raised:
        schedules.to_cron(**args)
    assert str(raised.value) == "invalid_schedule"


def test_next_fire_is_strictly_after_and_in_utc():
    after = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)  # exactly a fire time
    assert schedules.next_fire("0 2 * * *", "UTC", after) == datetime(2026, 10, 14, 2, 0, tzinfo=UTC)


def test_next_fire_follows_the_time_zone_and_dst():
    # 02:30 Berlin doesn't exist on 2026-03-29 (clocks jump 02:00 → 03:00): the next real time, never a skipped day.
    fire = schedules.next_fire("30 2 * * *", "Europe/Berlin", datetime(2026, 3, 28, 12, 0, tzinfo=UTC))
    assert fire.tzinfo is not None and fire.date().isoformat() == "2026-03-29"
    summer = schedules.next_fire("0 2 * * *", "Europe/Berlin", datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
    assert summer == datetime(2026, 7, 2, 0, 0, tzinfo=UTC)  # 02:00 CEST


def test_an_unknown_zone_is_invalid_timezone():
    with pytest.raises(InvalidInput) as raised:
        schedules.next_fire("0 2 * * *", "Mars/Olympus", datetime(2026, 1, 1, tzinfo=UTC))
    assert str(raised.value) == "invalid_timezone"


def test_next_fire_is_strictly_after_in_the_fall_back_hour():
    # 02:30 Berlin happens twice on 2026-10-25 (00:30Z, 01:30Z) and cronsim fires only the first: from 01:00Z that
    # is already past, so the next fire is the following day's 02:30 CET (01:30Z).
    after = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    fire = schedules.next_fire("30 2 * * *", "Europe/Berlin", after)
    assert fire > after
    assert fire == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)
