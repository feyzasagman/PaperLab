"""Fetching a paper's references and citing works, and folding rows that turn out to be one paper (M7.5)."""

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, text
from test_references_providers import PagedS2, recorded_references

from app.core import references
from app.core.errors import Conflict
from app.models import ExternalRef, Paper, paper_references
from app.providers import embedding, semantic_scholar

# Fetches and embeds take one advisory lock (core/references.py); in a test it lasts until the rollback, so these
# files share one xdist worker, or two of them deadlock on the same stored reference.
pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

READER_DOI = "10.5555/m75-reader"
REFS = f"/graph/v1/paper/DOI:{READER_DOI}/references"
CITING = f"/graph/v1/paper/DOI:{READER_DOI}/citations"
PDF = b"%PDF-1.7\n%a reference\n"
# How Semantic Scholar lists a reference it knows nothing about (the recorded BERT page 2 has four).
NO_IDS = {"paperId": None, "title": "Corpus of linguistic acceptability", "year": 2018, "venue": "", "authors": [],
          "externalIds": None, "openAccessPdf": None, "citationCount": None}  # fmt: skip


@pytest.fixture
async def library(session):
    """The dev database (D15) may hold the owner's references, note vectors and notes; hide them with a temp-table
    shadow (same trick as conftest.py's SHADOW_SEARCH_SOURCE) so counts and similarities are this test's own — not
    a DELETE, which on a 38k+ row external_refs forces a sequential scan of workspace_search_hits per deleted row
    to enforce its ON DELETE SET NULL foreign key (confirmed via EXPLAIN), and holds real locks other workers wait
    on until the rollback."""
    for table in ("paper_references", "note_embeddings", "external_refs", "notes"):
        await session.execute(text(f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL)"))
    return session


async def add_paper(session, **fields) -> Paper:
    paper = Paper(file_path="/nonexistent.pdf", **{"title": "A library paper", **fields})
    session.add(paper)
    await session.flush()
    return paper


def s2(title: str, doi: str | None = None, year: int = 2020, cites: int = 10, pdf: str = "", **ids) -> dict:
    external = {"DOI": doi} if doi else {}
    return {
        "paperId": ids.get("paper_id") or uuid.uuid5(uuid.NAMESPACE_URL, title).hex + "00000000",
        "title": title,
        "year": year,
        "venue": "Venue",
        "authors": [{"name": "Ada Lovelace"}],
        "externalIds": external,
        "citationCount": cites,
        "openAccessPdf": {"url": pdf},
    }


def page(field: str, *records: dict) -> dict:
    return {"offset": 0, "data": [{field: record} for record in records]}


async def stored(session, paper_id, direction="cites") -> list[str]:
    rows = await session.execute(
        select(ExternalRef.title)
        .join(paper_references, paper_references.c.ref_id == ExternalRef.id)
        .where(paper_references.c.paper_id == paper_id, paper_references.c.direction == direction)
        .order_by(paper_references.c.position)
    )
    return list(rows.scalars())


# --- fetch -------------------------------------------------------------------------------------------------------


async def test_both_directions_are_stored_and_citing_works_come_newest_first(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI)
    discovery_fakes.s2.reply(
        REFS, 200, json=page("citedPaper", s2("Older idea", "10.5555/a"), s2("Method", "10.5555/b"))
    )
    discovery_fakes.s2.reply(
        CITING, 200, json=page("citingPaper", s2("Follow-up 2019", year=2019), s2("Critique 2024", year=2024))
    )

    notices = await references.fetch(library, discovery_fakes.providers, reader)

    assert notices == []
    assert await stored(library, reader.id) == ["Older idea", "Method"]
    assert await stored(library, reader.id, "cited_by") == ["Critique 2024", "Follow-up 2019"]


async def test_openalex_is_merged_when_on_and_one_paper_is_one_row(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI, openalex_id="W9000000750")
    shared = {"id": "https://openalex.org/W9000000751", "doi": "https://doi.org/10.5555/shared", "title": "Shared"}
    discovery_fakes.openalex.route("/works/W9000000750", {"referenced_works": ["https://openalex.org/W9000000751"]})
    discovery_fakes.openalex.route("/works?openalex_id:W9000000751", {"results": [shared]})
    discovery_fakes.openalex.route("/works?cites:W9000000750", {"results": []})
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("Shared", "10.5555/SHARED"), s2("Only in S2")))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers, reader)

    assert sorted(await stored(library, reader.id)) == ["Only in S2", "Shared"]
    [row] = await library.scalars(select(ExternalRef).where(ExternalRef.title == "Shared"))
    assert (row.external_ids.get("openalex"), row.external_ids.get("semantic_scholar") is not None, row.doi) == (
        "W9000000751", True, "10.5555/shared",
    )  # fmt: skip


async def test_openalex_is_not_asked_when_it_is_off(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI, openalex_id="W9000000750")
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("A reference")))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers.without("openalex"), reader)

    assert discovery_fakes.openalex.requests == []
    assert await stored(library, reader.id) == ["A reference"]


async def test_one_failing_source_is_a_notice_beside_the_other_sources_rows(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI, openalex_id="W9000000750")
    discovery_fakes.openalex.route("/works/W9000000750", httpx.Response(503, text="unavailable"))
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("From Semantic Scholar")))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    notices = await references.fetch(library, discovery_fakes.providers, reader)

    assert notices == ["OpenAlex is busy or unreachable."]
    assert await stored(library, reader.id) == ["From Semantic Scholar"]


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("off", references.REFERENCES_OFF),
        ("unknown", references.REFERENCES_UNKNOWN),
        ("busy", "Semantic Scholar is busy or unreachable."),
    ],
)
async def test_no_answer_at_all_is_a_conflict_that_says_why(library, discovery_fakes, setup, message):
    reader = await add_paper(library, doi=READER_DOI)
    providers = discovery_fakes.providers.without("openalex")
    if setup == "off":
        providers = providers.without("semantic_scholar")
    elif setup == "unknown":
        discovery_fakes.s2.reply(REFS, 404, json={"error": "Paper not found"})
    else:
        discovery_fakes.s2.refuse(REFS)

    with pytest.raises(Conflict, match=f"^{message}$"):
        await references.fetch(library, providers, reader)


async def test_a_second_fetch_replaces_the_links_without_duplicating_references(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI)
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))
    discovery_fakes.s2.reply(
        REFS, 200, json=page("citedPaper", s2("Kept", "10.5555/kept"), s2("Dropped", "10.5555/gone"))
    )
    await references.fetch(library, discovery_fakes.providers, reader)
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("Kept, retitled", "10.5555/kept")))

    await references.fetch(library, discovery_fakes.providers, reader)

    assert await stored(library, reader.id) == ["Kept, retitled"]
    assert await library.scalar(select(func.count()).select_from(ExternalRef)) == 2  # the dropped row stays for others


async def test_two_stored_rows_found_to_be_one_paper_fold_into_the_oldest(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI)
    other = await add_paper(library, doi="10.5555/m75-other")
    by_s2 = ExternalRef(
        external_ids={"semantic_scholar": "a" * 40}, title="Known by id",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=1),
    )  # fmt: skip
    by_doi = ExternalRef(doi="10.5555/fold", title="Known by DOI")
    library.add_all([by_s2, by_doi])
    await library.flush()
    await library.execute(
        paper_references.insert().values(paper_id=other.id, ref_id=by_doi.id, direction="cites", position=0)
    )
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("One paper", "10.5555/fold", paper_id="a" * 40)))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers, reader)

    [row] = await library.scalars(select(ExternalRef))
    assert (row.id, row.external_ids.get("semantic_scholar"), row.doi) == (by_s2.id, "a" * 40, "10.5555/fold")
    assert await stored(library, other.id) == ["One paper"]  # the other paper's link moved to the kept row


async def test_duplicate_fold_preserves_pdf_urls_from_all_rows(library, discovery_fakes):
    """When two stored rows fold, the surviving row keeps PDF URLs from both the keeper and duplicates."""
    reader = await add_paper(library, doi=READER_DOI)
    # One row known by s2_id, holds a PDF URL
    by_s2 = ExternalRef(
        external_ids={"semantic_scholar": "b" * 40},
        doi="10.5555/m75-pdf-keep",
        title="Known by id with URL",
        pdf_urls=["https://example.org/kept.pdf"],
        fetched_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    # Another row known by doi, no URLs
    by_doi = ExternalRef(doi="10.5555/m75-pdf-keep", title="Known by DOI no URLs", pdf_urls=[])
    library.add_all([by_s2, by_doi])
    await library.flush()
    # Fetch returns a candidate with both IDs but no PDF URL
    discovery_fakes.s2.reply(
        REFS, 200, json=page("citedPaper", s2("Merged paper", "10.5555/m75-pdf-keep", paper_id="b" * 40, pdf=""))
    )
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers, reader)

    # After folding, exactly one row remains, and it still has the PDF URL
    [row] = await library.scalars(select(ExternalRef))
    assert row.external_ids.get("semantic_scholar") == "b" * 40
    assert row.doi == "10.5555/m75-pdf-keep"
    assert "https://example.org/kept.pdf" in row.pdf_urls


async def test_imported_row_wins_fold_and_keeps_imported_as(library, discovery_fakes):
    """When folding duplicates, an imported row (one with imported_as set) wins over non-imported rows."""
    reader = await add_paper(library, doi=READER_DOI)
    imported_paper = await add_paper(library, doi="10.5555/m75-imported-winner")
    # Older row without import
    older_unimported = ExternalRef(
        external_ids={"semantic_scholar": "c" * 40},
        doi="10.5555/m75-same-paper",
        title="Older, not imported",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    # Newer row with import (should win despite being newer due to import status)
    newer_imported = ExternalRef(
        external_ids={"openalex": "W9999999"},
        doi="10.5555/m75-same-paper",
        title="Newer, imported",
        imported_as=imported_paper.id,
        fetched_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    library.add_all([older_unimported, newer_imported])
    await library.flush()
    # Fetch returns a candidate with the s2_id, DOI, and openalex_id
    discovery_fakes.s2.reply(
        REFS,
        200,
        json=page(
            "citedPaper",
            s2("Merged paper", "10.5555/m75-same-paper", paper_id="c" * 40, openalex_id="W9999999"),
        ),
    )
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers, reader)

    # After folding, exactly one row remains: the imported one
    [row] = await library.scalars(select(ExternalRef))
    assert row.id == newer_imported.id
    assert row.imported_as == imported_paper.id
    assert row.external_ids.get("semantic_scholar") == "c" * 40  # merged from older row
    assert row.external_ids.get("openalex") == "W9999999"  # from newer row


async def test_recorded_references_with_no_id_are_skipped_and_every_other_one_is_stored(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI)
    pages = [recorded_references("s2_references_bert_page1"), recorded_references("s2_references_bert_page2")]
    fake = PagedS2()
    fake.page(REFS, 0, pages[0])
    fake.page(REFS, 40, pages[1])
    fake.page(CITING, 0, page("citingPaper"))
    records = [item["citedPaper"] for body in pages for item in body["data"]]
    with_an_id = [record["title"] for record in records if record["paperId"]]

    async with semantic_scholar.new_client(api_key=None, transport=fake.transport) as client:
        providers = replace(
            discovery_fakes.providers,
            clients={**discovery_fakes.providers.clients, "semantic_scholar": client, "openalex": None},
        )
        notices = await references.fetch(library, providers, reader)

    assert (notices, len(records), len(with_an_id)) == ([], 63, 59)
    assert await stored(library, reader.id) == with_an_id


async def test_a_record_with_no_id_leaves_other_papers_references_alone(library, discovery_fakes):
    other = await add_paper(library, doi="10.5555/m75-other")
    theirs = [ExternalRef(external_ids={"semantic_scholar": c * 40}, title=f"Their reference {c}") for c in "de"]
    library.add_all(theirs)
    await library.flush()
    await library.execute(
        paper_references.insert(),
        [{"paper_id": other.id, "ref_id": r.id, "direction": "cites", "position": i} for i, r in enumerate(theirs)],
    )
    reader = await add_paper(library, doi=READER_DOI)
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", NO_IDS, s2("Known", paper_id="c" * 40)))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper", NO_IDS))

    await references.fetch(library, discovery_fakes.providers, reader)

    assert await stored(library, other.id) == ["Their reference d", "Their reference e"]
    assert (await stored(library, reader.id), await stored(library, reader.id, "cited_by")) == (["Known"], [])
    assert await library.scalar(select(func.count()).select_from(ExternalRef)) == 3


async def test_a_row_stored_earlier_in_the_same_fetch_can_fold_into_an_older_one(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI)
    older = ExternalRef(
        external_ids={"semantic_scholar": "y" * 40}, title="Beta",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=1),
    )  # fmt: skip
    library.add(older)
    await library.flush()
    # The two Alpha records (same title and author) merge into one candidate, stored first as a new row with the DOI.
    # Beta shares that DOI, so storing Beta folds the new Alpha row into the older Beta row, after Alpha's link.
    beta = s2("Beta", "10.5555/m75-shared", paper_id="y" * 40) | {"authors": [{"name": "Grace Hopper"}]}
    alpha, alpha_with_doi = s2("Alpha", paper_id="x" * 40), s2("Alpha", "10.5555/m75-shared", paper_id="z" * 40)
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", alpha, beta, alpha_with_doi))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers, reader)

    [row] = await library.scalars(select(ExternalRef))
    assert (row.id, row.doi) == (older.id, "10.5555/m75-shared")
    assert await stored(library, reader.id) == ["Beta"]


# --- one writer at a time ----------------------------------------------------------------------------------------

HOLDS_AN_ADVISORY_LOCK = text(
    "SELECT count(*) > 0 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted"
)


async def test_references_are_stored_under_the_references_lock(library, discovery_fakes, monkeypatch):
    reader = await add_paper(library, doi=READER_DOI)
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("A reference")))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))
    upsert, locked = references._upsert, []

    async def upsert_noting_the_lock(session, candidate):
        locked.append(await session.scalar(HOLDS_AN_ADVISORY_LOCK))
        return await upsert(session, candidate)

    monkeypatch.setattr(references, "_upsert", upsert_noting_the_lock)
    await references.fetch(library, discovery_fakes.providers, reader)

    assert locked == [True]


async def test_vectors_are_written_under_the_references_lock(library, embedder, monkeypatch):
    library.add(ExternalRef(title="Needs a vector", queued_at=func.now()))
    await library.flush()
    embed, locked = embedding.embed_documents, []

    async def embed_noting_the_lock(model, texts):
        locked.append(await library.scalar(HOLDS_AN_ADVISORY_LOCK))
        return await embed(model, texts)

    monkeypatch.setattr(embedding, "embed_documents", embed_noting_the_lock)
    await references.embed_new(library, embedder)

    assert locked == [True]


# --- To read across a fold and a Refresh (D120, D164) ------------------------------------------------------------

EARLIER, LATER = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("case", ["keeper_queued_later", "only_the_duplicate_queued", "keeper_imported_unqueued"])
async def test_a_fold_keeps_the_earliest_to_read_whichever_row_survives(library, discovery_fakes, case):
    reader = await add_paper(library, doi=READER_DOI)
    imported = await add_paper(library, doi="10.5555/m21-imported") if case == "keeper_imported_unqueued" else None
    keeper = ExternalRef(
        external_ids={"semantic_scholar": "e" * 40},
        title="Kept",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=2),
        queued_at=LATER if case == "keeper_queued_later" else None,
        imported_as=imported.id if imported else None,
    )
    duplicate = ExternalRef(doi="10.5555/m21-fold", title="Folded", queued_at=EARLIER)
    library.add_all([keeper, duplicate])
    await library.flush()
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("One paper", "10.5555/m21-fold", paper_id="e" * 40)))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))

    await references.fetch(library, discovery_fakes.providers, reader)

    [(row_id, queued_at)] = (await library.execute(select(ExternalRef.id, ExternalRef.queued_at))).all()
    assert (row_id, queued_at) == (keeper.id, EARLIER)


async def test_a_refresh_keeps_to_read_even_when_it_was_pressed_mid_refresh(library, discovery_fakes):
    reader = await add_paper(library, doi=READER_DOI)
    discovery_fakes.s2.reply(REFS, 200, json=page("citedPaper", s2("Kept To read", "10.5555/m21-kept")))
    discovery_fakes.s2.reply(CITING, 200, json=page("citingPaper"))
    await references.fetch(library, discovery_fakes.providers, reader)
    [ref_id] = await library.scalars(select(ExternalRef.id))
    first = await references.queue(library, ref_id)

    await references.fetch(library, discovery_fakes.providers, reader)  # a plain Refresh
    assert await library.scalar(select(ExternalRef.queued_at).where(ExternalRef.id == ref_id)) == first

    # To read pressed while a Refresh runs: the Refresh's session loaded the row before the press, so its copy still
    # says NULL. text(), not update(ExternalRef): the ORM would synchronise the loaded copy and hide the bug.
    await references.unqueue(library, ref_id)
    stale = await library.get(ExternalRef, ref_id)
    assert stale.queued_at is None
    await library.execute(
        text("UPDATE external_refs SET queued_at = :at WHERE id = :id"), {"at": EARLIER, "id": ref_id}
    )

    await references.fetch(library, discovery_fakes.providers, reader)

    assert await library.scalar(select(ExternalRef.queued_at).where(ExternalRef.id == ref_id)) == EARLIER
