import logging

import pytest
from sqlalchemy import delete

from app.core import paper_sources
from app.core.errors import InvalidInput
from app.models import PaperSources

pytestmark = pytest.mark.anyio

KEY = "sk-core-0123456789"


@pytest.fixture
async def no_row(session):
    # The dev database (D15) holds the owner's own row; hide it inside the test's rolled-back transaction.
    await session.execute(delete(PaperSources))
    return session


async def test_without_a_row_every_source_is_on_but_openalex(no_row):
    sources = await paper_sources.get(no_row)

    assert sources.enabled == {
        "openalex": False, "crossref": True, "semantic_scholar": True, "arxiv": True, "core": True,
        "unpaywall": True, "pubmed": True, "pmc": True, "europe_pmc": True, "zenodo": True, "hal": True,
        "acm_dl": True, "ssrn": False, "doaj": True, "openaire": True, "iacr_eprint": True,
        "biorxiv": True,
        "medrxiv": True, "dblp": False,
    }  # fmt: skip
    assert sources.contact_email is None
    assert sources.api_keys == {
        "openalex": None, "semantic_scholar": None, "core": None, "pubmed": None, "pmc": None, "zenodo": None,
    }  # fmt: skip
    assert not sources.unpaywall_on  # ticked, but Unpaywall refuses requests without an email


async def test_an_openalex_mailto_seeds_the_email_and_turns_openalex_on_once(no_row, caplog):
    caplog.set_level(logging.INFO)

    assert await paper_sources.seed_from_env(no_row, " me@example.org ", "s2-key-0123456789") is True
    assert await paper_sources.seed_from_env(no_row, "", "") is False  # a row exists: .env is ignored from now on

    sources = await paper_sources.get(no_row)
    assert (sources.contact_email, sources.enabled["openalex"]) == ("me@example.org", True)
    assert sources.api_keys["semantic_scholar"] == "s2-key-0123456789"
    assert "s2-key-0123456789" not in caplog.text


async def test_an_unusable_openalex_mailto_seeds_with_no_email_and_openalex_off(no_row, caplog):
    caplog.set_level(logging.WARNING)
    bad_mailto = "m" * 300

    assert await paper_sources.seed_from_env(no_row, bad_mailto, "") is True

    sources = await paper_sources.get(no_row)
    assert (sources.contact_email, sources.enabled["openalex"]) == (None, False)
    assert bad_mailto not in caplog.text


async def test_an_empty_env_seeds_the_defaults(no_row):
    assert await paper_sources.seed_from_env(no_row, "", "") is True

    assert await paper_sources.get(no_row) == paper_sources.SourceSettings()


async def test_only_what_is_sent_changes(no_row):
    await paper_sources.update(no_row, {"contact_email": "me@example.org", "api_keys": {"core": f"  {KEY}  "}})

    sources = await paper_sources.update(no_row, {"enabled": {"openalex": True, "crossref": False}})

    assert (sources.contact_email, sources.api_keys["core"]) == ("me@example.org", KEY)  # kept, and stripped
    assert (sources.enabled["openalex"], sources.enabled["crossref"], sources.enabled["arxiv"]) == (True, False, True)
    assert sources.unpaywall_on


async def test_null_removes_the_email_or_a_key(no_row):
    await paper_sources.update(no_row, {"contact_email": "me@example.org", "api_keys": {"core": KEY}})

    sources = await paper_sources.update(no_row, {"contact_email": None, "api_keys": {"core": None}})

    assert (sources.contact_email, sources.api_keys["core"]) == (None, None)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"contact_email": "not an email"}, paper_sources.BAD_EMAIL),
        ({"contact_email": "me@localhost"}, paper_sources.BAD_EMAIL),
        ({"contact_email": f"{'a' * 250}@example.org"}, paper_sources.BAD_EMAIL),
        ({"api_keys": {"core": "   "}}, paper_sources.EMPTY_KEY),
        ({"api_keys": {"core": "sk-​core-0123456789"}}, paper_sources.NOT_PLAIN_KEY),
        ({"api_keys": {"core": "sk-core\n0123456789"}}, paper_sources.NOT_PLAIN_KEY),
        ({"api_keys": {"arxiv": KEY}}, "arxiv doesn't take an API key"),
        ({"enabled": {"unknown_source": True}}, "unknown paper source: unknown_source"),
    ],
    ids=[
        "no-at", "no-dot", "too-long", "blank-key", "zero-width-space", "newline", "keyless-source", "unknown-source",
    ],  # fmt: skip
)
async def test_bad_changes_are_refused_and_change_nothing(no_row, changes, message):
    with pytest.raises(InvalidInput, match=message):
        await paper_sources.update(no_row, {"enabled": {"crossref": False}, **changes})

    assert (await paper_sources.get(no_row)).enabled["crossref"] is True


async def test_the_view_never_carries_a_key(no_row):
    sources = await paper_sources.update(no_row, {"api_keys": {"core": KEY, "openalex": "short"}})

    view = paper_sources.view(sources)

    assert [s.id for s in view.sources] == list(paper_sources.SOURCES)
    by_id = {s.id: s for s in view.sources}
    assert (by_id["core"].has_key, by_id["core"].key_hint) == (True, "6789")
    assert (by_id["openalex"].has_key, by_id["openalex"].key_hint) == (True, None)  # under 8 characters: no hint
    assert (by_id["semantic_scholar"].has_key, by_id["arxiv"].has_key) == (False, None)  # None: takes no key
    assert KEY not in repr(view) and "short" not in repr(view)
