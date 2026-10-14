import uuid

import pytest
from sqlalchemy import text, update

from app.config import settings
from app.core import papers
from app.core.chunking import ChunkDraft
from app.models import Paper, PaperStatus

pytestmark = pytest.mark.anyio


async def upload(client, name="paper.pdf", data=b"%PDF-1.7\nbody"):
    return await client.post("/api/papers", files={"file": (name, data, "application/pdf")})


async def test_upload_creates_paper_and_enqueues_ingest(client, arq, pdf_dir):
    response = await upload(client, "Attention Is All You Need.pdf")

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Attention Is All You Need"
    assert body["status"] == "uploaded"
    assert arq.jobs == [("ingest_paper", body["id"])]
    assert (pdf_dir / f"{body['id']}.pdf").exists()


async def test_upload_non_pdf_is_rejected(client, arq, pdf_dir):
    response = await upload(client, "notes.txt", b"not a pdf")

    assert response.status_code == 422
    assert "not a PDF" in response.json()["detail"]
    assert arq.jobs == []
    assert not pdf_dir.exists() or not any(pdf_dir.iterdir())


async def test_list_returns_newest_first(client, session):
    first = (await upload(client, "first.pdf")).json()
    second = (await upload(client, "second.pdf")).json()
    # The test transaction freezes now(), so both rows share created_at; age the first one.
    await session.execute(
        update(Paper).where(Paper.id == uuid.UUID(first["id"])).values(created_at=text("now() - interval '1 day'"))
    )

    ids = [p["id"] for p in (await client.get("/api/papers")).json()]
    assert ids.index(second["id"]) < ids.index(first["id"])


async def test_get_paper(client):
    created = (await upload(client)).json()

    response = await client.get(f"/api/papers/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


# Fixed ids: pytest-xdist workers must collect identical test names, and the random UUID would differ per worker.
@pytest.mark.parametrize(
    ("path", "status"),
    [(f"/api/papers/{uuid.uuid4()}", 404), ("/api/papers/not-a-uuid", 422)],
    ids=["missing", "not-a-uuid"],
)
async def test_get_paper_errors(client, path, status):
    assert (await client.get(path)).status_code == status


async def test_chunks_filter_by_page(client, session):
    created = (await upload(client)).json()
    paper_id = uuid.UUID(created["id"])
    await papers.replace_chunks(
        session,
        paper_id,
        [
            ChunkDraft(ordinal=0, page=1, bbox=[[1, 2, 3, 4]], section_title=None, text="page one"),
            ChunkDraft(ordinal=1, page=2, bbox=[[1, 2, 3, 4], [5, 6, 7, 8]], section_title="Intro", text="page two"),
        ],
    )

    all_chunks = (await client.get(f"/api/papers/{paper_id}/chunks")).json()
    page_two = (await client.get(f"/api/papers/{paper_id}/chunks", params={"page": 2})).json()

    assert [c["ordinal"] for c in all_chunks] == [0, 1]
    assert [(c["text"], c["section_title"], c["bbox"]) for c in page_two] == [
        ("page two", "Intro", [[1, 2, 3, 4], [5, 6, 7, 8]])
    ]


async def test_chunks_of_unknown_paper_is_404(client):
    assert (await client.get(f"/api/papers/{uuid.uuid4()}/chunks")).status_code == 404


async def test_reingest_enqueues_job(client, arq, session):
    created = (await upload(client)).json()
    paper_id = uuid.UUID(created["id"])
    # A paper reads `ready` before a re-ingest is requested, same as any paper due for a refresh.
    await papers.set_status(session, paper_id, PaperStatus.READY)
    arq.jobs.clear()

    response = await client.post(f"/api/papers/{created['id']}/reingest")

    assert response.status_code == 202
    # The status flips to `uploaded` immediately, so the chat panel's "ask again once ready" is true
    # the instant reingest is requested, not just once the worker gets around to it.
    assert response.json()["status"] == "uploaded"
    assert arq.jobs == [("ingest_paper", created["id"])]
    refreshed = await client.get(f"/api/papers/{created['id']}")
    assert refreshed.json()["status"] == "uploaded"


async def test_reingest_unknown_paper_is_404_and_enqueues_nothing(client, arq):
    response = await client.post(f"/api/papers/{uuid.uuid4()}/reingest")

    assert response.status_code == 404
    assert arq.jobs == []


async def test_health_vector_roundtrip(client):
    body = (await client.get("/api/health")).json()
    assert {k: body[k] for k in ("orm_roundtrip", "raw_sql_nearest_is_self")} == {
        "orm_roundtrip": True, "raw_sql_nearest_is_self": True
    }


async def test_health_names_the_stack(client, monkeypatch):
    monkeypatch.setattr(settings, "paperlab_stack", "paperlab-e2e")
    assert (await client.get("/api/health")).json()["stack"] == "paperlab-e2e"


async def test_a_paper_shows_its_enrichment_metadata(client):
    created = (await upload(client)).json()

    assert {k: created[k] for k in ("openalex_id", "type", "is_retracted", "oa_status", "oa_url", "issn")} == {
        "openalex_id": None,
        "type": None,
        "is_retracted": False,
        "oa_status": None,
        "oa_url": None,
        "issn": None,
    }
    assert (created["cited_by_count"], created["referenced_works_count"], created["authors"]) == (None, None, [])


async def test_correcting_a_paper_persists(client):
    created = (await upload(client)).json()
    correction = {"title": " BERT ", "authors": ["Jacob Devlin", "Ming-Wei Chang"], "year": 2019, "is_retracted": True}

    response = await client.patch(f"/api/papers/{created['id']}", json=correction)

    assert response.status_code == 200
    reloaded = (await client.get(f"/api/papers/{created['id']}")).json()
    assert reloaded == response.json()
    assert (reloaded["title"], reloaded["authors"], reloaded["year"], reloaded["is_retracted"]) == (
        "BERT",
        ["Jacob Devlin", "Ming-Wei Chang"],
        2019,
        True,
    )


async def test_correcting_a_paper_errors(client, session):
    created = (await upload(client)).json()
    session.add(Paper(title="taken", file_path="/other.pdf", doi="10.1000/taken"))
    await session.commit()
    url = f"/api/papers/{created['id']}"

    assert (await client.patch(f"/api/papers/{uuid.uuid4()}", json={"title": "x"})).status_code == 404
    response = await client.patch(url, json={"doi": "10.1000/TAKEN"})
    assert (response.status_code, response.json()) == (409, {"detail": "doi_taken"})  # I6: every error status
    for body in [{}, {"title": "   "}, {"title": None}, {"authors": ["Ada", " "]}, {"year": 99}, {"doi": "nope"}]:
        response = await client.patch(url, json=body)
        assert response.status_code == 422, body


# The abstract is correctable via the same PATCH, string or null to clear.


async def test_correcting_a_paper_abstract(client):
    created = (await upload(client)).json()
    assert created["abstract"] is None

    response = await client.patch(f"/api/papers/{created['id']}", json={"abstract": "A corrected abstract."})

    assert response.status_code == 200
    assert response.json()["abstract"] == "A corrected abstract."

    cleared = await client.patch(f"/api/papers/{created['id']}", json={"abstract": None})
    assert (cleared.status_code, cleared.json()["abstract"]) == (200, None)


async def test_patch_refuses_the_reading_state_even_beside_a_real_correction(client, session):
    """D163: {"reading_pass": 2} alone already answers 422 (nothing to correct), so each body carries a real title."""
    created = (await upload(client)).json()
    url = f"/api/papers/{created['id']}"
    assert (await client.put(f"{url}/reading", json={"reading_pass": 1})).status_code == 200

    for field, value in (("reading_pass", 2), ("triage", "keep")):
        response = await client.patch(url, json={"title": created["title"], field: value})
        assert response.status_code == 422, field

    paper = await session.get(Paper, uuid.UUID(created["id"]), populate_existing=True)
    assert (paper.reading_pass, paper.triage, paper.manual_fields) == (1, None, [])
