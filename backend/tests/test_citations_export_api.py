import uuid

import pytest

from app.core import pipeline
from app.core.errors import InvalidInput, NotFound
from app.models import Paper

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

RUN = uuid.uuid4().hex[:8]


async def make_paper(session, title, **kw) -> Paper:
    kw.setdefault("authors", ["Jane Smith"])
    paper = Paper(title=title, file_path="/nonexistent.pdf", page_count=2, year=2021, **kw)
    session.add(paper)
    await session.commit()
    return paper


async def workspace_with(client, session, *titles):
    workspace = (await client.post("/api/workspaces", json={"name": f"Cite ws {RUN} é"})).json()
    papers = [await make_paper(session, t, source_type="journal_article") for t in titles]
    for paper in papers:
        await client.put(f"/api/workspaces/{workspace['id']}/papers/{paper.id}")
    return workspace, papers


async def test_workspace_download_has_type_filename_and_every_paper(client, session):
    workspace, papers = await workspace_with(client, session, f"Alpha {RUN}", f"Beta {RUN}")
    response = await client.get(f"/api/workspaces/{workspace['id']}/citations?format=bibtex")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-bibtex")
    assert response.headers["content-disposition"] == f'attachment; filename="cite-ws-{RUN}-e.bib"'
    assert f"Alpha {RUN}" in response.text and f"Beta {RUN}" in response.text and response.text.count("@article") == 2
    await client.delete(f"/api/workspaces/{workspace['id']}")


async def test_every_format_over_http(client, session):
    workspace, _ = await workspace_with(client, session, f"Gamma {RUN}")
    for fmt, ext in [("ris", "ris"), ("csljson", "json"), ("apa", "txt"), ("apa-html", "html")]:
        response = await client.get(f"/api/workspaces/{workspace['id']}/citations", params={"format": fmt})
        assert response.status_code == 200 and f"Gamma {RUN}" in response.text, fmt
        assert response.headers["content-disposition"].endswith(f'.{ext}"'), fmt
    await client.delete(f"/api/workspaces/{workspace['id']}")


async def test_selection_export_keeps_the_given_order_and_names_the_file_citations(client, session):
    a, b = await make_paper(session, f"Aaa {RUN}"), await make_paper(session, f"Bbb {RUN}")
    response = await client.post(
        "/api/citations/export", json={"paper_ids": [str(b.id), str(a.id), str(b.id)], "format": "ris"}
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="citations.ris"'
    assert response.text.index(f"Bbb {RUN}") < response.text.index(f"Aaa {RUN}")
    assert response.text.count("TY  - ") == 2


async def test_http_errors(client, session):
    workspace, _ = await workspace_with(client, session, f"Delta {RUN}")
    missing = uuid.uuid4()
    paper = await make_paper(session, f"Eps {RUN}")
    ws = f"/api/workspaces/{workspace['id']}/citations"
    empty = (await client.post("/api/workspaces", json={"name": f"Empty {RUN}"})).json()
    post = "/api/citations/export"
    many = [str(uuid.uuid4())] * 1001
    cases = [
        await client.get(f"/api/workspaces/{missing}/citations?format=ris"),
        await client.get(ws + "?format=docx"),
        await client.get(ws),
        await client.get(f"/api/workspaces/{empty['id']}/citations?format=ris"),
        await client.post(post, json={"paper_ids": [], "format": "ris"}),
        await client.post(post, json={"paper_ids": [str(paper.id)], "format": "docx"}),
        await client.post(post, json={"paper_ids": many, "format": "ris"}),
        await client.post(post, json={"paper_ids": [str(missing)], "format": "ris"}),
    ]
    assert [r.status_code for r in cases] == [404, 422, 422, 422, 422, 422, 422, 404]
    assert cases[1].json()["detail"] == "unknown_format" and cases[3].json()["detail"] == "no_papers"
    for w in (workspace, empty):
        await client.delete(f"/api/workspaces/{w['id']}")


async def test_pipeline_export_citations_returns_text_and_count(client, session):
    workspace, papers = await workspace_with(client, session, f"Zeta {RUN}", f"Eta {RUN}")
    text, count = await pipeline.export_citations(session, workspace_id=uuid.UUID(workspace["id"]), fmt="csljson")
    assert count == 2 and f"Zeta {RUN}" in text
    text, count = await pipeline.export_citations(session, paper_ids=[papers[0].id], fmt="apa")
    assert count == 1 and f"Zeta {RUN}" in text
    with pytest.raises(InvalidInput, match="unknown_format"):
        await pipeline.export_citations(session, paper_ids=[papers[0].id], fmt="docx")
    with pytest.raises(InvalidInput, match="no_papers"):
        await pipeline.export_citations(session, fmt="ris")
    with pytest.raises(NotFound):
        await pipeline.export_citations(session, workspace_id=uuid.uuid4(), fmt="ris")
    await client.delete(f"/api/workspaces/{workspace['id']}")
