import httpx
import pytest

from app.core import enrichment
from app.providers import crossref
from app.scripts import backfill_source_types
from tests.test_enrichment import add_paper, hints

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

DOI = "10.9999/f6.test"


def fake_crossref(work=None, status=200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, text="nope")
        return httpx.Response(200, json={"message": work})

    return crossref.new_client(email="me@example.org", transport=httpx.MockTransport(handler)), seen


async def enrich(session, paper, client):
    await enrichment.enrich_paper(session, None, paper.id, hints(), crossref_http=client)
    await session.refresh(paper)
    return paper


async def test_crossref_type_and_published_version_fill_the_paper(session):
    paper = await add_paper(session, doi=DOI)
    client, seen = fake_crossref({"type": "posted-content", "relation": {"is-preprint-of": [{"id": "10.1234/PUB"}]}})
    await enrich(session, paper, client)
    assert (paper.source_type, paper.peer_reviewed, paper.published_doi) == ("preprint", "false", "10.1234/pub")
    assert seen[0].url.params["mailto"] == "me@example.org"


async def test_a_messy_doi_is_normalised_before_the_call(session):
    paper = await add_paper(session, doi="https://doi.org/10.9999/F6.UPPER")
    client, seen = fake_crossref({"type": "journal-article"})
    await enrich(session, paper, client)
    assert paper.source_type == "journal_article" and seen[0].url.path == "/works/10.9999/f6.upper"


@pytest.mark.parametrize("status", [404, 429, 500])
async def test_crossref_failure_leaves_fields_and_does_not_raise(session, status):
    paper = await add_paper(session, doi=DOI)
    client, _ = fake_crossref(None, status)
    await enrich(session, paper, client)
    assert (paper.source_type, paper.peer_reviewed) == (None, "unknown")


async def test_crossref_network_error_is_tolerated(session):
    def boom(request):
        raise httpx.ConnectError("down")

    paper = await add_paper(session, doi=DOI)
    await enrich(session, paper, crossref.new_client(transport=httpx.MockTransport(boom)))
    assert paper.source_type is None


async def test_an_owner_edit_survives_enrichment(session):
    paper = await add_paper(
        session,
        doi=DOI,
        source_type="magazine",
        peer_reviewed="unknown",
        manual_fields=["source_type", "peer_reviewed"],
    )
    client, _ = fake_crossref({"type": "journal-article"})
    await enrich(session, paper, client)
    assert (paper.source_type, paper.peer_reviewed) == ("magazine", "unknown")


async def test_a_locked_source_type_leaves_peer_review_alone(session):
    paper = await add_paper(session, doi=DOI, source_type="book", manual_fields=["source_type"])
    await enrich(session, paper, fake_crossref({"type": "journal-article"})[0])
    assert (paper.source_type, paper.peer_reviewed) == ("book", "unknown")


async def test_an_arxiv_doi_is_a_preprint_without_asking_crossref(session):
    paper = await add_paper(session, doi="10.48550/arxiv.1706.03762")
    client, seen = fake_crossref({"type": "journal-article"})
    await enrich(session, paper, client)
    assert (paper.source_type, paper.peer_reviewed, seen) == ("preprint", "false", [])


async def test_a_known_source_type_is_not_asked_again(session):
    paper = await add_paper(session, doi=DOI, source_type="book")
    client, seen = fake_crossref({"type": "journal-article"})
    await enrich(session, paper, client)
    assert (paper.source_type, seen) == ("book", [])


async def test_backfill_maps_from_type_or_crossref_skips_manual_and_is_idempotent(session):
    by_type = await add_paper(session, type="conference-paper", doi="10.9999/bf.1")
    by_crossref = await add_paper(session, doi="10.9999/bf.2")
    locked = await add_paper(session, doi="10.9999/bf.3", source_type="magazine", manual_fields=["source_type"])
    client, seen = fake_crossref({"type": "book-chapter"})
    ids = [by_type.id, by_crossref.id, locked.id]

    first = await backfill_source_types.run(session, client, delay=0, only=ids)
    second = await backfill_source_types.run(session, client, delay=0, only=ids)

    for paper in (by_type, by_crossref, locked):
        await session.refresh(paper)
    assert [p.source_type for p in (by_type, by_crossref, locked)] == ["proceedings", "chapter", "magazine"]
    assert first["updated"] == 2 and second["updated"] == 0
    assert len(seen) == 1  # only by_crossref ever asked, and the second run found it done


def raw_crossref(body):
    return crossref.new_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))


@pytest.mark.parametrize(
    "body", [{}, {"status": "ok"}, {"message": {"type": "posted-content", "relation": ["x"]}}, {"message": []}]
)
async def test_an_odd_crossref_reply_leaves_fields_and_does_not_raise(session, body):
    paper = await add_paper(session, doi=DOI)
    await enrich(session, paper, raw_crossref(body))
    assert (paper.source_type, paper.published_doi) == (None, None)


async def test_published_doi_is_recorded_even_when_the_source_type_is_locked(session):
    paper = await add_paper(session, doi=DOI, source_type=None, manual_fields=["source_type"])
    client, _ = fake_crossref({"type": "posted-content", "relation": {"is-preprint-of": [{"id": "10.1234/pub"}]}})
    await enrich(session, paper, client)
    assert (paper.source_type, paper.published_doi) == (None, "10.1234/pub")
