"""Saved searches CRUD (M39 F2)."""

import uuid
from datetime import datetime, timezone

import pytest

from app.core import saved_searches, workspaces
from app.core.errors import DomainError, InvalidInput, NotFound

pytestmark = pytest.mark.anyio

UTC = timezone.utc
RUN = uuid.uuid4().hex[:8]


async def test_create_computes_the_next_run_and_lists_it(session):
    ws = await workspaces.create(session, f"Saved {RUN}")
    now = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    view = await saved_searches.create(session, ws.id, query="llm creativity", schedule="0 2 * * 1",
                                       timezone="UTC", sources=["arxiv"], year_from=2023, year_to=2026, now=now)
    assert view.next_run_at == datetime(2026, 10, 19, 2, 0, tzinfo=UTC)
    assert (view.schedule_kind, view.time, view.weekday) == ("weekly", "02:00", 1)
    assert [v.id for v in await saved_searches.list_for_workspace(session, ws.id)] == [view.id]


async def test_disabling_clears_next_run_and_enabling_recomputes_it_from_now(session):
    ws = await workspaces.create(session, f"Toggle {RUN}")
    t0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    view = await saved_searches.create(session, ws.id, query="q", schedule="0 2 * * *", timezone="UTC", now=t0)
    off = await saved_searches.update(session, ws.id, view.id, {"enabled": False}, now=t0)
    on = await saved_searches.update(session, ws.id, view.id, {"enabled": True},
                                     now=datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    assert off.next_run_at is None
    assert on.next_run_at == datetime(2026, 10, 21, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize("bad, code", [
    ({"query": "  "}, "invalid_input"), ({"sources": ["google_scholar"]}, "unknown_source"),
    ({"year_from": 2026, "year_to": 2020}, "invalid_years"), ({"schedule": "nope"}, "invalid_schedule"),
    ({"timezone": "Nowhere/City"}, "invalid_timezone"),
])
async def test_bad_fields_are_refused(session, bad, code):
    ws = await workspaces.create(session, f"Bad {code} {RUN}")
    fields = {"query": "q", "schedule": "0 2 * * *", "timezone": "UTC", **bad}
    with pytest.raises(DomainError) as raised:
        await saved_searches.create(session, ws.id, **fields)
    assert str(raised.value) == code or (code == "invalid_input" and isinstance(raised.value, InvalidInput))


async def test_another_workspaces_saved_search_is_not_found(session):
    mine = await workspaces.create(session, f"Mine {RUN}")
    theirs = await workspaces.create(session, f"Theirs {RUN}")
    view = await saved_searches.create(session, theirs.id, query="q", schedule="0 2 * * *", timezone="UTC")
    with pytest.raises(NotFound):
        await saved_searches.update(session, mine.id, view.id, {"enabled": False})
    with pytest.raises(NotFound):
        await saved_searches.delete(session, mine.id, view.id)


async def test_a_refused_update_of_a_disabled_search_changes_nothing(session):
    ws = await workspaces.create(session, f"Disabled {RUN}")
    view = await saved_searches.create(session, ws.id, query="q", schedule="0 2 * * *", timezone="UTC")
    await saved_searches.update(session, ws.id, view.id, {"enabled": False})
    with pytest.raises(InvalidInput) as raised:
        await saved_searches.update(session, ws.id, view.id, {"enabled": False, "timezone": "Nowhere/City"})
    assert str(raised.value) == "invalid_timezone"
    assert (await saved_searches.get(session, view.id)).timezone == "UTC"
