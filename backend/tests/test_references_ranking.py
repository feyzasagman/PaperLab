"""Asking for a fetch, embedding titles and notes, and ranking references for the library (M7.5)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeEmbedder, unit_vector
from sqlalchemy import delete, func, select, update

from app.core import references
from app.core.errors import NotFound
from app.models import ExternalRef, Note, NoteEmbedding, Paper, paper_references

# Fetches and embeds take one advisory lock (core/references.py); in a test it lasts until the rollback, so these
# files share one xdist worker, or two of them deadlock on the same stored reference.
pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

READER_DOI = "10.5555/m75-reader"
REFS = f"/graph/v1/paper/DOI:{READER_DOI}/references"
CITING = f"/graph/v1/paper/DOI:{READER_DOI}/citations"
PDF = b"%PDF-1.7\n%a reference\n"


@pytest.fixture
async def library(session):
    """The dev database (D15) may hold the owner's references, note vectors and notes; hide them in this test's
    rolled-back transaction so counts and similarities are this test's own."""
    for model in (paper_references, NoteEmbedding, ExternalRef, Note):
        await session.execute(delete(model))
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


# --- request_fetch -----------------------------------------------------------------------------------------------


async def test_a_second_request_while_fetching_queues_nothing_until_it_goes_stale(library):
    reader = await add_paper(library, doi=READER_DOI)

    assert await references.request_fetch(library, reader.id) is True
    assert await references.request_fetch(library, reader.id) is False
    await library.execute(
        update(Paper)
        .where(Paper.id == reader.id)
        .values(references_requested_at=func.now() - references.FETCH_STALE * 2)
    )
    assert await references.request_fetch(library, reader.id) is True
    with pytest.raises(NotFound):
        await references.request_fetch(library, uuid.uuid4())


async def test_a_fetch_the_worker_never_finished_is_listed_as_failed_without_writing_it(library):
    now = datetime.now(timezone.utc)
    running = await add_paper(library, doi=READER_DOI, references_state="fetching", references_requested_at=now)
    stale = now - references.FETCH_STALE * 2
    lost = await add_paper(library, doi="10.5555/m75-lost", references_state="fetching", references_requested_at=stale)

    listed = [await references.listing(library, paper.id, "cites") for paper in (running, lost)]

    assert [(listing.state, listing.error) for listing in listed] == [
        ("fetching", None), ("failed", references.FETCH_FAILED)
    ]  # fmt: skip
    stored = await library.scalar(select(Paper.references_state).where(Paper.id == lost.id))
    assert stored == "fetching"


# --- embeddings --------------------------------------------------------------------------------------------------


async def test_only_missing_or_stale_vectors_are_embedded(library, embedder):
    reader = await add_paper(library, doi=READER_DOI)
    library.add_all(
        [ExternalRef(title="Needs a vector", queued_at=func.now()), Note(body="a note about attention", provenance="human")]
    )
    await library.flush()

    await references.embed_new(library, embedder)
    await references.embed_new(library, embedder)  # nothing new

    assert [texts for texts, _ in embedder.calls] == [
        ["search_document: Needs a vector"],
        ["search_document: a note about attention"],
    ]
    await library.execute(update(Note).values(body="edited", updated_at=func.now() + timedelta(seconds=1)))
    await references.embed_new(library, embedder)
    assert embedder.calls[-1][0] == ["search_document: edited"]
    assert reader.id  # the paper itself is untouched


async def test_only_the_references_a_listing_ranks_are_embedded_in_bounded_batches(library, embedder, monkeypatch):
    """Harvests insert thousands of references no paper cites; embedding them all under the lock stalled every fetch."""
    monkeypatch.setattr(references, "EMBED_BATCH", 2)
    reader = await add_paper(library, doi=READER_DOI)
    linked = [ExternalRef(title=f"Cited {i}") for i in range(5)]
    queued = ExternalRef(title="Queued", queued_at=func.now())
    harvested = ExternalRef(title="Harvested, nobody cites it")
    library.add_all([*linked, queued, harvested])
    await library.flush()
    for position, ref in enumerate(linked):
        await library.execute(
            paper_references.insert().values(paper_id=reader.id, ref_id=ref.id, direction="cites", position=position)
        )

    await references.embed_new(library, embedder)

    sizes = [len(texts) for texts, _ in embedder.calls]
    assert sizes == [2, 2, 2] and sum(sizes) == 6
    embedded = {r for r in await library.scalars(select(ExternalRef.title).where(ExternalRef.title_embedding.is_not(None)))}
    assert harvested.title not in embedded and len(embedded) == 6


async def test_a_cloud_source_embeds_notes_and_titles_as_a_local_one_does_under_its_own_name(library):
    """P3 = A (owner, D161): every search source, cloud or not, embeds notes and reference titles for the ranking.
    Their vectors record the source's name, so a switch embeds them again like the papers."""
    name = "openai/text-embedding-3-small@768"
    cloud = FakeEmbedder(name=name, label="OpenAI", is_local=False)
    ref = ExternalRef(title="Needs a vector", queued_at=func.now())
    note = Note(body="a note about attention", provenance="human")
    library.add_all([ref, note])
    await library.flush()

    await references.embed_new(library, cloud)

    assert [texts for texts, _ in cloud.calls] == [
        ["search_document: Needs a vector"],
        ["search_document: a note about attention"],
    ]  # nomic's prefixes: FakeEmbedder keeps them whatever its name
    assert await library.scalar(select(NoteEmbedding.embed_model).where(NoteEmbedding.note_id == note.id)) == name
    assert await library.scalar(select(ExternalRef.title_embed_model).where(ExternalRef.id == ref.id)) == name


async def test_with_no_search_model_nothing_is_embedded(library):
    """D136: the references tab still ranks, by co-citation, PDFs and citations; note similarity is simply absent."""
    library.add_all(
        [ExternalRef(title="Needs a vector", queued_at=func.now()), Note(body="a note about attention", provenance="human")]
    )
    await library.flush()

    await references.embed_new(library, None)

    assert await library.scalar(select(func.count()).select_from(NoteEmbedding)) == 0
    assert await library.scalar(select(func.count(ExternalRef.title_embedding))) == 0


# --- ranking -----------------------------------------------------------------------------------------------------


async def link(session, paper, *refs, direction="cites"):
    await session.execute(
        paper_references.insert(),
        [{"paper_id": paper.id, "ref_id": r.id, "direction": direction, "position": i} for i, r in enumerate(refs)],
    )


async def test_a_reference_two_library_papers_cite_outranks_a_famous_uncited_one(library):
    reader = await add_paper(library, doi=READER_DOI)
    second = await add_paper(library, doi="10.5555/m75-second")
    famous = ExternalRef(title="Famous", cited_by_count=100_000)
    shared = ExternalRef(title="Shared", cited_by_count=5)
    library.add_all([famous, shared])
    await library.flush()
    await link(library, reader, famous, shared)
    await link(library, second, shared)

    listing = await references.listing(library, reader.id, "cites")

    assert [(r.title, r.cocitation) for r in listing.rows] == [("Shared", 2), ("Famous", 1)]


async def test_ties_break_on_closeness_to_notes_then_a_free_pdf_then_citations(library):
    reader = await add_paper(library, doi=READER_DOI)
    note = Note(body="n", provenance="human")
    library.add(note)
    await library.flush()
    near, far = unit_vector("near"), unit_vector("far")
    library.add(NoteEmbedding(note_id=note.id, embedding=near, embed_model="test", noted_at=datetime.now(timezone.utc)))
    close = ExternalRef(title="Close to a note", title_embedding=near, title_embed_model="test", cited_by_count=1)
    with_pdf = ExternalRef(
        title="Has a PDF", title_embedding=far, title_embed_model="test", pdf_urls=["https://x/p.pdf"]
    )
    cited = ExternalRef(title="Cited more", title_embedding=far, title_embed_model="test", cited_by_count=900)
    library.add_all([close, with_pdf, cited])
    await library.flush()
    await link(library, reader, cited, with_pdf, close)

    listing = await references.listing(library, reader.id, "cites")

    assert [r.title for r in listing.rows] == ["Close to a note", "Has a PDF", "Cited more"]


async def test_the_summary_counts_references_cited_three_times_and_those_with_pdfs(library):
    papers_ = [await add_paper(library, doi=f"10.5555/m75-{i}") for i in range(3)]
    common = ExternalRef(title="Common", pdf_urls=["https://x/p.pdf"])
    rare = ExternalRef(title="Rare")
    library.add_all([common, rare])
    await library.flush()
    for paper in papers_:
        await link(library, paper, common)
    await link(library, papers_[0], rare)

    listing = await references.listing(library, papers_[0].id, "cites")

    assert (listing.summary.cited_by_3plus, listing.summary.with_pdf) == (1, 1)
    assert listing.state == "none"


async def test_a_reference_uploaded_after_the_fetch_still_shows_in_library(library):
    reader = await add_paper(library, doi=READER_DOI)
    ref = ExternalRef(title="Later uploaded", doi="10.5555/M75-Later")
    library.add(ref)
    await library.flush()
    await link(library, reader, ref)
    uploaded = await add_paper(library, doi="10.5555/m75-later")

    [row] = (await references.listing(library, reader.id, "cites")).rows

    assert row.paper_id == uploaded.id


async def test_a_reference_with_an_uppercase_arxiv_id_is_in_library_under_its_lowercase_arxiv_doi(library):
    reader = await add_paper(library, doi=READER_DOI)
    arxiv_id = f"cs.CL/{uuid.uuid4().hex[:8]}"  # old-style arXiv IDs carry the archive's capitals
    ref = ExternalRef(title="An old arXiv paper", external_ids={"arxiv": arxiv_id})
    library.add(ref)
    await library.flush()
    await link(library, reader, ref)
    uploaded = await add_paper(library, doi=f"10.48550/arxiv.{arxiv_id.lower()}")

    [row] = (await references.listing(library, reader.id, "cites")).rows

    assert row.paper_id == uploaded.id
