"""Saved-search schedules (M39 F2, D222/D223): one 5-field cron string read in an IANA time zone. Daily and weekly
are cron shapes; the UI and MCP convert to and from them."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

from app.core.errors import InvalidInput

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SHAPE = re.compile(r"^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$")
_HINT = "A 5-field cron expression, e.g. '0 2 * * 1'."


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidInput("invalid_timezone", timezone=name) from None


def _check(cron: str) -> str:
    try:
        CronSim(cron, datetime.now(timezone.utc))
    except CronSimError:
        raise InvalidInput("invalid_schedule", hint=_HINT) from None
    return cron


def to_cron(kind: str, time: str | None = None, weekday: int | None = None, cron: str | None = None) -> str:
    """daily → 'M H * * *', weekly → 'M H * * D' (0 = Sunday), cron → checked as is. Raises
    InvalidInput("invalid_schedule")."""
    if kind == "cron":
        return _check(" ".join((cron or "").split()))
    match = _TIME.match(time or "")
    if kind not in ("daily", "weekly") or not match or (kind == "weekly" and weekday not in range(7)):
        raise InvalidInput("invalid_schedule", hint="daily or weekly at HH:MM (weekday 0 = Sunday … 6), or cron.")
    hour, minute = int(match[1]), int(match[2])
    return f"{minute} {hour} * * {'*' if kind == 'daily' else weekday}"


def describe(cron: str) -> tuple[str, str | None, int | None]:
    """('daily' | 'weekly' | 'custom', 'HH:MM' or None, weekday or None) — how the UI shows a stored schedule."""
    match = _SHAPE.match(cron)
    if not match or int(match[2]) > 23 or int(match[1]) > 59:
        return "custom", None, None
    time = f"{int(match[2]):02d}:{int(match[1]):02d}"
    return ("daily", time, None) if match[3] == "*" else ("weekly", time, int(match[3]))


def next_fire(cron: str, tz: str, after: datetime) -> datetime:
    """The first fire time strictly after `after`, in UTC. Raises InvalidInput (invalid_schedule / invalid_timezone)."""
    zone = _zone(tz)
    try:
        fire = next(CronSim(cron, after.astimezone(zone)))
        if fire.astimezone(timezone.utc) <= after:
            fire = next(CronSim(cron, after.astimezone(zone) + timedelta(seconds=1)))
    except CronSimError:
        raise InvalidInput("invalid_schedule", hint=_HINT) from None
    return fire.astimezone(timezone.utc)
