import csv
import io
import uuid

import pytest
from pdf_papers import chunked_paper
from sqlalchemy import insert
from test_evidence_cards import FULL, INTRO, Scripted, card_reply

from app.core import evidence_cards, queue, retrieval
from app.models import Workspace, workspace_papers

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


@pytest.fixture(autouse=True)
def wiring(embedder, arq, monkeypatch):
    async def query_embedder(s, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)
    monkeypatch.setattr(queue, "_pool", arq)


async def setup(session, tmp_path, built=True):
    paper = (await chunked_paper(session, tmp_path, title="Recall study", intro=INTRO))[0]
    ws = Workspace(name=f"Api {uuid.uuid4().hex[:8]}")
    session.add(ws)
    await session.flush()
    await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=paper.id))
    if built:
        await evidence_cards.build_card(session, paper.id, llm=Scripted(card_reply(**FULL)))
    return paper, ws


async def test_get_returns_the_card_and_404_when_there_is_none(client, session, tmp_path):
    paper, _ = await setup(session, tmp_path)
    body = (await client.get(f"/api/papers/{paper.id}/evidence-card")).json()
    assert body["status"] == "ready" and body["fields"]["sample"]["n"] == 120 and body["provenance"] == "llm"
    bare, _ = await setup(session, tmp_path / "b", built=False)
    assert (await client.get(f"/api/papers/{bare.id}/evidence-card")).status_code == 404
    assert (await client.get(f"/api/papers/{uuid.uuid4()}/evidence-card")).status_code == 404


async def test_post_queues_generation_and_returns_202_with_the_view(client, session, arq, tmp_path):
    paper, _ = await setup(session, tmp_path, built=False)
    response = await client.post(f"/api/papers/{paper.id}/evidence-card", json={})
    assert response.status_code == 202 and response.json()[0]["status"] == "generating"
    assert [j[0] for j in arq.jobs] == ["generate_evidence_cards"]
    assert (await client.post(f"/api/papers/{uuid.uuid4()}/evidence-card", json={})).status_code == 404


async def test_post_regenerating_an_edited_card_needs_confirm(client, session, arq, tmp_path):
    paper, _ = await setup(session, tmp_path)
    await evidence_cards.edit_card(session, paper.id, [{"field": "design", "values": {"value": "mine"}}])
    url = f"/api/papers/{paper.id}/evidence-card"
    refused = await client.post(url, json={"regenerate": True})
    assert refused.status_code == 409 and arq.jobs == []
    assert (await client.post(url, json={"regenerate": True, "confirm": True})).status_code == 202


async def test_patch_edits_values_and_rejects_quote_changes(client, session, tmp_path):
    paper, _ = await setup(session, tmp_path)
    url = f"/api/papers/{paper.id}/evidence-card"
    ok = await client.patch(url, json={"changes": [{"field": "design", "values": {"value": "Mine"}}]})
    assert ok.status_code == 200 and ok.json()["provenance"] == "llm_edited"
    assert ok.json()["fields"]["design"]["flags"] == ["edited"]
    bad = await client.patch(url, json={"changes": [{"field": "design", "values": {"quote": "a b c"}}]})
    assert bad.status_code == 422
    assert (await client.patch(f"/api/papers/{uuid.uuid4()}/evidence-card", json={"changes": []})).status_code == 404


async def test_workspace_table_lists_papers_with_cards_or_null(client, session, tmp_path):
    paper, ws = await setup(session, tmp_path / "a")
    bare = (await chunked_paper(session, tmp_path / "b", title="Zeta"))[0]
    await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=bare.id))
    rows = (await client.get(f"/api/workspaces/{ws.id}/evidence-cards")).json()
    assert [r["title"] for r in rows] == ["Recall study", "Zeta"]
    assert rows[0]["card"]["status"] == "ready" and rows[1]["card"] is None
    assert (await client.get(f"/api/workspaces/{uuid.uuid4()}/evidence-cards")).status_code == 404


async def test_workspace_post_queues_only_papers_without_a_ready_card(client, session, arq, tmp_path):
    paper, ws = await setup(session, tmp_path / "a")
    bare = (await chunked_paper(session, tmp_path / "b", title="Zeta"))[0]
    await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=bare.id))
    url = f"/api/workspaces/{ws.id}/evidence-cards"
    response = await client.post(url, json={})
    assert response.status_code == 202 and len(response.json()) == 2
    assert arq.jobs == [("generate_evidence_cards", str(ws.id), [str(bare.id)])]
    assert (await client.post(f"/api/workspaces/{uuid.uuid4()}/evidence-cards", json={})).status_code == 404


async def test_csv_download_has_bom_header_and_a_filename(client, session, tmp_path):
    _, ws = await setup(session, tmp_path)
    response = await client.get(f"/api/workspaces/{ws.id}/evidence-cards.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][:3] == ["paper", "year", "field"] and any(r[2] == "sample" for r in rows[1:])
    assert (await client.get(f"/api/workspaces/{uuid.uuid4()}/evidence-cards.csv")).status_code == 404
