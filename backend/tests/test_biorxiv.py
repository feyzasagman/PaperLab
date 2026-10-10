import httpx
import pytest
from conftest import FakeProvider

from app.providers import biorxiv

pytestmark = pytest.mark.anyio


def _page_body(entries: list[dict], *, cursor: int, total: int) -> dict:
    return {
        "messages": [{"status": "ok", "cursor": cursor, "count": len(entries), "total": str(total)}],
        "collection": entries,
    }


def _entry(doi: str, title: str = "A Preprint") -> dict:
    return {
        "title": title,
        "authors": "Last, First; Other, Second",
        "doi": doi,
        "date": "2026-10-01",
        "abstract": "An abstract.",
        "published": "NA",
        "server": "bioRxiv",
    }


@pytest.fixture
async def biorxiv_api():
    fake = FakeProvider()
    fake.client = biorxiv.new_client(transport=fake.transport)
    yield fake
    await fake.client.aclose()


async def test_fetch_page_returns_entries_and_the_next_cursor(biorxiv_api):
    biorxiv_api.reply(
        "/details/biorxiv/2026-10-01/2026-10-10/0",
        200,
        json=_page_body([_entry("10.1101/2026.10.01.000001")], cursor=0, total=50),
    )

    entries, next_cursor = await biorxiv.fetch_page(
        biorxiv_api.client, "biorxiv", from_date="2026-10-01", until_date="2026-10-10", cursor=0
    )

    assert len(entries) == 1
    assert entries[0]["doi"] == "10.1101/2026.10.01.000001"
    assert next_cursor == 1  # cursor(0) + len(entries)(1) < total(50)


async def test_fetch_page_returns_none_cursor_when_exhausted(biorxiv_api):
    biorxiv_api.reply(
        "/details/biorxiv/2026-10-01/2026-10-10/0",
        200,
        json=_page_body([_entry("10.1101/2026.10.01.000001")], cursor=0, total=1),
    )

    _, next_cursor = await biorxiv.fetch_page(
        biorxiv_api.client, "biorxiv", from_date="2026-10-01", until_date="2026-10-10", cursor=0
    )

    assert next_cursor is None  # cursor(0) + len(entries)(1) is not < total(1)


async def test_fetch_page_requests_the_right_path_for_medrxiv(biorxiv_api):
    biorxiv_api.reply(
        "/details/medrxiv/2026-01-01/2026-01-02/30",
        200,
        json=_page_body([], cursor=30, total=30),
    )

    await biorxiv.fetch_page(biorxiv_api.client, "medrxiv", from_date="2026-01-01", until_date="2026-01-02", cursor=30)

    assert biorxiv_api.requests[0].url.path == "/details/medrxiv/2026-01-01/2026-01-02/30"


async def test_no_posts_found_is_not_an_error(biorxiv_api):
    """Confirmed live 2026-10-10: a date range with genuinely zero matching papers, and a cursor past
    the real total, both answer {"status": "no posts found"} -- a legitimate empty result, not a failure."""
    biorxiv_api.reply(
        "/details/biorxiv/2030-01-01/2030-01-02/0",
        200,
        json={"messages": [{"status": "no posts found"}], "collection": []},
    )

    entries, next_cursor = await biorxiv.fetch_page(
        biorxiv_api.client, "biorxiv", from_date="2030-01-01", until_date="2030-01-02", cursor=0
    )

    assert entries == []
    assert next_cursor is None


async def test_any_other_status_raises_an_httpx_error(biorxiv_api):
    biorxiv_api.reply(
        "/details/biorxiv/2026-10-01/2026-10-10/0",
        200,
        json={"messages": [{"status": "something unexpected"}], "collection": []},
    )

    with pytest.raises(httpx.HTTPError):
        await biorxiv.fetch_page(
            biorxiv_api.client, "biorxiv", from_date="2026-10-01", until_date="2026-10-10", cursor=0
        )


@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_every_failure_status_raises_an_httpx_error(biorxiv_api, status):
    biorxiv_api.reply("/details/biorxiv/2026-10-01/2026-10-10/0", status, json={"detail": "boom"})

    with pytest.raises(httpx.HTTPError):
        await biorxiv.fetch_page(
            biorxiv_api.client, "biorxiv", from_date="2026-10-01", until_date="2026-10-10", cursor=0
        )
