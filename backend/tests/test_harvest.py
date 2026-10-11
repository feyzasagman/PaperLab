from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.core.candidates import Candidate
from app.models import ExternalRef
from app.models.harvest import HarvestCursor
from app.workers.harvest import harvest_ingest_candidate

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _reuse_test_session(session):
    yield session  # never closes it -- the `session` fixture's own teardown does that


async def test_a_new_candidate_is_inserted(session):
    candidate = Candidate(
        title="A New Eprint", authors=["Ada Fixture"], year=2026, external_ids={"iacr_eprint": "2026/9001"},
        abstract="An abstract.", sources=("iacr_eprint",),
    )

    await harvest_ingest_candidate(session, candidate)
    await session.flush()

    row = (
        await session.execute(select(ExternalRef).where(ExternalRef.external_ids["iacr_eprint"].astext == "2026/9001"))
    ).scalar_one()
    assert row.title == "A New Eprint"
    assert row.sources == ["iacr_eprint"]


async def test_a_revised_eprint_updates_the_existing_row_not_a_duplicate(session):
    first = Candidate(
        title="Version One", authors=[], year=2026, external_ids={"iacr_eprint": "2026/9002"}, sources=("iacr_eprint",),
    )
    await harvest_ingest_candidate(session, first)
    await session.flush()

    revised = Candidate(
        title="Version Two (revised)", authors=["New Author"], year=2026,
        external_ids={"iacr_eprint": "2026/9002"}, sources=("iacr_eprint",),
    )
    await harvest_ingest_candidate(session, revised)
    await session.flush()

    rows = (
        await session.execute(select(ExternalRef).where(ExternalRef.external_ids["iacr_eprint"].astext == "2026/9002"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Version Two (revised)"
    assert rows[0].authors == ["New Author"]


def test_harvest_iacr_eprint_is_registered_as_a_cron_job():
    from app.workers.settings import WorkerSettings

    names = {job.name for job in WorkerSettings.cron_jobs}
    assert "harvest_iacr_eprint" in names


def test_harvest_biorxiv_is_registered_as_a_cron_job():
    from app.workers.settings import WorkerSettings

    names = {job.name for job in WorkerSettings.cron_jobs}
    assert "harvest_biorxiv" in names


def test_harvest_medrxiv_is_registered_as_a_cron_job():
    from app.workers.settings import WorkerSettings

    names = {job.name for job in WorkerSettings.cron_jobs}
    assert "harvest_medrxiv" in names


async def test_running_the_harvest_twice_does_not_duplicate_rows(session, monkeypatch):
    from app.providers import iacr_eprint
    from app.workers import harvest

    call_count = 0
    from_dates = []

    async def fake_fetch_page(http, *, from_date=None, until_date=None, resumption_token=None):
        nonlocal call_count
        call_count += 1
        from_dates.append(from_date)
        if call_count == 1:
            return [{"identifier": "oai:eprint.iacr.org:2026/5001", "datestamp": "2026-01-01T00:00:00Z",
                      "title": "Only Paper", "creators": [], "description": None}], None
        return [], None  # second run: nothing new since last_synced_at

    monkeypatch.setattr(iacr_eprint, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(harvest, "SessionLocal", lambda: _reuse_test_session(session))

    await harvest.harvest_iacr_eprint({})
    await harvest.harvest_iacr_eprint({})

    from sqlalchemy import select

    from app.models import ExternalRef

    rows = (
        await session.execute(select(ExternalRef).where(ExternalRef.external_ids["iacr_eprint"].astext == "2026/5001"))
    ).scalars().all()
    assert len(rows) == 1
    assert call_count == 2  # the second run made exactly one fetch_page call, not a full re-harvest
    assert from_dates[0] == "1996-01-01"  # first run starts from the beginning of time (no prior cursor)
    assert from_dates[1] != "1996-01-01"  # second run resumed from the advanced cursor, not from scratch


async def test_hitting_the_page_ceiling_with_a_pending_token_raises_and_does_not_advance_the_cursor(
    session, monkeypatch
):
    """If the loop exhausts MAX_HARVEST_PAGES while a resumption token is still pending, silently falling
    through would truncate the harvest and still advance the cursor, losing the un-fetched remainder for
    good. It must raise instead, so harvest_iacr_eprint's own except/rollback keeps the cursor from
    advancing and the next run resumes from the same point."""
    from app.providers import iacr_eprint
    from app.workers import harvest

    monkeypatch.setattr(harvest, "MAX_HARVEST_PAGES", 2)

    async def fake_fetch_page(http, *, from_date=None, until_date=None, resumption_token=None):
        # every page still returns a token: the chain never finishes within the lowered page budget
        return ([{"identifier": "oai:eprint.iacr.org:2026/7001", "datestamp": "2026-01-01T00:00:00Z",
                   "title": "Page", "creators": [], "description": None}], "always-more")  # fmt: skip

    monkeypatch.setattr(iacr_eprint, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(harvest, "SessionLocal", lambda: _reuse_test_session(session))

    # This shares the dev DB with real harvest runs (see conftest's own TEST_DATABASE_URL comment), so a
    # real cursor row may already exist here -- compare before/after rather than assuming None.
    before = await session.get(HarvestCursor, "iacr_eprint")
    before_synced_at = before.last_synced_at if before else None

    with pytest.raises(RuntimeError, match="MAX_HARVEST_PAGES"):
        await harvest.harvest_iacr_eprint({})

    session.expire_all()
    after = await session.get(HarvestCursor, "iacr_eprint")
    assert (after.last_synced_at if after else None) == before_synced_at


async def test_the_harvest_follows_a_multi_page_resumption_chain_to_completion(session, monkeypatch):
    from app.providers import iacr_eprint
    from app.workers import harvest

    pages = [
        ([{"identifier": "oai:eprint.iacr.org:2026/6001", "datestamp": "2026-01-01T00:00:00Z", "title": "Page 1",
           "creators": [], "description": None}], "token-page-2"),
        ([{"identifier": "oai:eprint.iacr.org:2026/6002", "datestamp": "2026-01-01T00:00:00Z", "title": "Page 2",
           "creators": [], "description": None}], None),
    ]
    calls = iter(pages)

    async def fake_fetch_page(http, *, from_date=None, until_date=None, resumption_token=None):
        return next(calls)

    monkeypatch.setattr(iacr_eprint, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(harvest, "SessionLocal", lambda: _reuse_test_session(session))

    await harvest.harvest_iacr_eprint({})

    from sqlalchemy import select

    from app.models import ExternalRef

    rows = (
        await session.execute(select(ExternalRef).where(ExternalRef.external_ids["iacr_eprint"].astext.isnot(None)))
    ).scalars().all()
    assert {r.external_ids["iacr_eprint"] for r in rows} == {"2026/6001", "2026/6002"}


async def test_a_record_with_no_doi_is_skipped_not_crashed_on(session, monkeypatch):
    from app.providers import biorxiv
    from app.workers import harvest

    async def fake_fetch_page(http, server, *, from_date=None, until_date=None, cursor=0):
        return [
            {"title": "No DOI Here", "authors": "Last, First", "doi": None,
             "date": "2026-10-01", "abstract": None, "published": "NA", "server": "bioRxiv"},
            {"title": "Has A DOI", "authors": "Last, First", "doi": "10.1101/2026.10.01.999099",
             "date": "2026-10-01", "abstract": None, "published": "NA", "server": "bioRxiv"},
        ], None

    monkeypatch.setattr(biorxiv, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(harvest, "SessionLocal", lambda: _reuse_test_session(session))

    await harvest.harvest_biorxiv({})

    from sqlalchemy import select

    from app.models import ExternalRef

    rows = (
        await session.execute(select(ExternalRef).where(ExternalRef.external_ids["biorxiv"].astext.isnot(None)))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].external_ids["biorxiv"] == "10.1101/2026.10.01.999099"
