import pytest
from sqlalchemy import delete

from app import main
from app.config import settings
from app.models import PaperSources

pytestmark = pytest.mark.anyio

KEY = "sk-openalex-0123456789"


@pytest.fixture
async def no_row(session):
    # The dev database (D15) holds the owner's own row; hide it inside the test's rolled-back transaction.
    await session.execute(delete(PaperSources))


async def test_the_defaults_come_back_without_a_row(client, no_row):
    response = await client.get("/api/paper-sources")

    assert response.status_code == 200
    body = response.json()
    assert body["contact_email"] is None
    assert [(s["id"], s["name"], s["enabled"], s["has_key"]) for s in body["sources"]] == [
        ("openalex", "OpenAlex", False, False),
        ("crossref", "Crossref", True, None),
        ("semantic_scholar", "Semantic Scholar", True, False),
        ("arxiv", "arXiv", True, None),
        ("core", "CORE", True, False),
        ("unpaywall", "Unpaywall", True, None),
        ("pubmed", "PubMed", True, False),
        ("pmc", "PMC", True, False),
        ("europe_pmc", "Europe PMC", True, None),
        ("zenodo", "Zenodo", True, False),
        ("hal", "HAL", True, None),
        ("acm_dl", "ACM DL", True, None),
        ("ssrn", "SSRN", False, None),
        ("doaj", "DOAJ", True, None),
        ("openaire", "OpenAIRE", True, None),
        ("iacr_eprint", "IACR ePrint", True, None),
        ("biorxiv", "bioRxiv", True, None),
        ("medrxiv", "medRxiv", True, None),
    ]


async def test_a_patch_changes_only_what_it_sends_and_never_returns_the_key(client, no_row):
    first = await client.patch(
        "/api/paper-sources",
        json={"contact_email": "me@example.org", "enabled": {"openalex": True}, "api_keys": {"openalex": KEY}},
    )
    second = await client.patch("/api/paper-sources", json={"enabled": {"crossref": False}})
    reread = await client.get("/api/paper-sources")

    assert (first.status_code, second.status_code) == (200, 200)
    openalex = second.json()["sources"][0]
    assert (openalex["enabled"], openalex["has_key"], openalex["key_hint"]) == (True, True, "6789")
    assert second.json()["contact_email"] == "me@example.org"
    assert second.json()["sources"][1]["enabled"] is False
    assert reread.json() == second.json()
    assert all(KEY not in r.text for r in (first, second, reread))


async def test_null_removes_a_key_and_the_email(client, no_row):
    await client.patch("/api/paper-sources", json={"contact_email": "me@example.org", "api_keys": {"core": KEY}})

    response = await client.patch("/api/paper-sources", json={"contact_email": None, "api_keys": {"core": None}})

    assert response.json()["contact_email"] is None
    assert response.json()["sources"][4]["has_key"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"contact_email": "not an email"},
        {"api_keys": {"core": "   "}},
        {"api_keys": {"core": ""}},
        {"api_keys": {"core": "sk-​core-0123456789"}},
        {"api_keys": {"arxiv": KEY}},
        {"enabled": {"dblp": True}},
        {"enabled": {"crossref": None}},
        {"enabled": {"crossref": "yes please"}},
        {"surprise": True},
    ],
    ids=[
        "bad-email", "blank-key", "empty-key", "non-ascii-key", "keyless-source", "unknown-source", "null-switch",
        "not-a-bool", "unknown-field",
    ],  # fmt: skip
)
async def test_a_bad_patch_is_422_and_never_echoes_a_key(client, no_row, body):
    response = await client.patch("/api/paper-sources", json={**body, "enabled": body.get("enabled", {"arxiv": False})})

    assert response.status_code == 422
    assert KEY not in response.text
    for key in body.get("api_keys", {}).values():
        if key:
            assert key not in response.text
    assert (await client.get("/api/paper-sources")).json()["sources"][3]["enabled"] is True


async def test_startup_seeds_the_row_from_env(monkeypatch):
    monkeypatch.setattr(settings, "openalex_mailto", "me@example.org")
    monkeypatch.setattr(settings, "semantic_scholar_api_key", "s2-key-0123456789")
    seeded = []

    class Pool:
        async def aclose(self):
            pass

    async def create_pool(_):
        return Pool()

    async def no_connections(session, *values):
        return False

    async def seed(session, *values):
        seeded.append(values)
        return True

    monkeypatch.setattr(main, "create_pool", create_pool)
    monkeypatch.setattr(main.llm_connections, "seed_from_env", no_connections)
    monkeypatch.setattr(main.paper_sources, "seed_from_env", seed)

    async with main.lifespan(main.create_app()):
        pass

    assert seeded == [("me@example.org", "s2-key-0123456789")]
