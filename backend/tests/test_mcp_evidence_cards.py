"""M39 F5 MCP tools: generate_evidence_card, get_evidence_card, list_evidence_cards."""

import uuid

import pytest
from mcp_fixtures import *  # noqa: F403
from mcp_fixtures import call, result
from pdf_papers import chunked_paper
from sqlalchemy import insert
from test_evidence_cards import FULL, INTRO, Scripted, card_reply

from app.core import evidence_cards, retrieval, screening, workspaces
from app.models import workspace_papers

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]
RUN = uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def search_with_the_test_embedder(embedder, monkeypatch):
    async def query_embedder(session, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)


def _model(monkeypatch, local: bool):
    async def default(_session):
        return ("Test · model", local, "localhost" if local else "api.example.com")

    monkeypatch.setattr(screening, "_default_model", default)


async def ready_card(session, tmp_path, reply=FULL):
    paper = (await chunked_paper(session, tmp_path, title="Recall study", intro=INTRO))[0]
    paper.authors, paper.year = ["Ada Lovelace"], 2021
    await evidence_cards.build_card(session, paper.id, llm=Scripted(card_reply(**reply)))
    return paper


async def test_generate_returns_at_once_with_status_generating_and_queues_one_job(session, jobs, tmp_path, monkeypatch):
    _model(monkeypatch, True)
    paper = (await chunked_paper(session, tmp_path, intro=INTRO))[0]
    card = result(await call("generate_evidence_card", paper_id=str(paper.id)))
    assert card["status"] == "generating" and card["paper_id"] == str(paper.id)
    assert [j[0] for j in jobs.jobs] == ["generate_evidence_cards"]
    again = result(await call("generate_evidence_card", paper_id=str(paper.id)))
    assert again["status"] == "generating" and len(jobs.jobs) == 1


async def test_a_remote_model_is_refused_until_allow_remote(session, jobs, tmp_path, monkeypatch):
    _model(monkeypatch, False)
    paper = (await chunked_paper(session, tmp_path, intro=INTRO))[0]
    refused = await call("generate_evidence_card", paper_id=str(paper.id))
    assert refused.structured_content["error"] == "remote_model_needs_confirm" and jobs.jobs == []
    ok = result(await call("generate_evidence_card", paper_id=str(paper.id), allow_remote=True))
    assert ok["status"] == "generating"


async def test_regenerating_an_edited_card_needs_confirm_and_an_unknown_paper_is_not_found(
    session, jobs, tmp_path, monkeypatch
):
    _model(monkeypatch, True)
    paper = await ready_card(session, tmp_path)
    await evidence_cards.edit_card(session, paper.id, [{"field": "design", "values": {"value": "mine"}}])
    refused = await call("generate_evidence_card", paper_id=str(paper.id), regenerate=True)
    assert refused.structured_content["error"] == "card_edited_needs_confirm"
    ok = result(await call("generate_evidence_card", paper_id=str(paper.id), regenerate=True, confirm=True))
    assert ok["status"] == "generating"
    missing = await call("generate_evidence_card", paper_id=str(uuid.uuid4()))
    assert missing.structured_content["error"] == "paper_not_found"


async def test_get_returns_kept_items_and_counts_the_ones_left_out(session, tmp_path):
    reply = {**FULL, "limitations": [{"value": "Invented", "quote": "nothing like this in the text"}]}
    paper = await ready_card(session, tmp_path, reply)
    card = result(await call("get_evidence_card", paper_id=str(paper.id)))
    assert card["status"] == "ready" and card["fields"]["sample"]["n"] == 120
    assert card["fields"]["limitations"] == [] and card["left_out"] == {"limitations": 1}
    assert "Invented" not in str(card)
    for items in (card["fields"]["findings"], card["fields"]["effect_sizes"]):
        assert all(i["value"] and i["quote"] and i["page"] is not None for i in items)


async def test_get_a_missing_card_is_card_not_found(session, tmp_path):
    paper = (await chunked_paper(session, tmp_path))[0]
    missing = await call("get_evidence_card", paper_id=str(paper.id))
    assert missing.structured_content["error"] == "card_not_found"


async def test_list_gives_one_row_per_paper_with_its_card_or_none(session, tmp_path):
    paper = await ready_card(session, tmp_path / "a")
    bare = (await chunked_paper(session, tmp_path / "b", title="Zeta"))[0]
    ws = await workspaces.create(session, f"Cards {RUN}")
    for p in (paper, bare):
        await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=p.id))
    rows = result(await call("list_evidence_cards", workspace=f"Cards {RUN}"))
    assert [(r["title"], r["first_author"], r["year"]) for r in rows] == [
        ("Recall study", "Ada Lovelace", 2021), ("Zeta", None, None)]
    assert rows[0]["card"]["status"] == "ready" and rows[1]["card"] is None
    unknown = await call("list_evidence_cards", workspace="Nope " + RUN)
    assert unknown.structured_content["error"] == "unknown_workspace"
