import pytest

from app.models import Paper

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


async def make(session, **fields) -> Paper:
    paper = Paper(title="F6 api", file_path="/nonexistent.pdf", **fields)
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return paper


async def test_paper_out_has_the_new_fields_and_derives_evidence_access(client, session):
    bare = await make(session)
    with_abstract = await make(
        session, abstract="An abstract", source_type="preprint", peer_reviewed="false", published_doi="10.1234/pub"
    )

    first = (await client.get(f"/api/papers/{bare.id}")).json()
    second = (await client.get(f"/api/papers/{with_abstract.id}")).json()

    assert (first["source_type"], first["peer_reviewed"], first["evidence_access"], first["published_doi"]) == (
        None,
        "unknown",
        "metadata",
        None,
    )
    assert (second["source_type"], second["peer_reviewed"], second["evidence_access"], second["published_doi"]) == (
        "preprint",
        "false",
        "abstract",
        "10.1234/pub",
    )


async def test_a_ready_paper_without_chunks_is_not_full_text_and_one_with_chunks_is(client, session, tmp_path):
    from pdf_papers import chunked_paper

    ready, _, _ = await chunked_paper(session, tmp_path)
    empty = await make(session, abstract="x", status="ready")
    ingesting, _, _ = await chunked_paper(session, tmp_path)
    ingesting.status = "embedding"
    await session.commit()
    for paper in (ready, ingesting):
        await session.refresh(paper)  # the test shares one session with the app; a real request loads fresh

    got = [(await client.get(f"/api/papers/{p.id}")).json()["evidence_access"] for p in (ready, empty, ingesting)]

    assert got == ["full_text", "abstract", "metadata"]  # still ingesting (no abstract) is never full_text


async def test_patch_sets_source_type_and_peer_reviewed_and_locks_them(client, session):
    paper = await make(session)

    response = await client.patch(f"/api/papers/{paper.id}", json={"source_type": "magazine", "peer_reviewed": "false"})

    assert response.status_code == 200
    body = response.json()
    assert (body["source_type"], body["peer_reviewed"]) == ("magazine", "false")
    await session.refresh(paper)
    assert {"source_type", "peer_reviewed"} <= set(paper.manual_fields)


@pytest.mark.parametrize("body", [{"source_type": "blog"}, {"peer_reviewed": "maybe"}, {"peer_reviewed": None}])
async def test_patch_rejects_bad_values(client, session, body):
    paper = await make(session)
    assert (await client.patch(f"/api/papers/{paper.id}", json=body)).status_code == 422
