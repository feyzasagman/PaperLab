import csv
import io
import uuid

import pytest
from pdf_papers import chunked_paper
from sqlalchemy import insert, select
from test_evidence_cards import FULL, INTRO, Scripted, card_reply

from app.core import evidence_cards, retrieval
from app.core.errors import Conflict, InvalidInput, NotFound
from app.models import EvidenceCard, Workspace, workspace_papers

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


@pytest.fixture(autouse=True)
def wiring(embedder, monkeypatch):
    async def query_embedder(s, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)


async def card_in_workspace(session, tmp_path, reply=FULL):
    paper = (await chunked_paper(session, tmp_path, title="Recall study", intro=INTRO))[0]
    paper.authors, paper.year = ["Ada Lovelace", "Bo Chen"], 2021
    ws = Workspace(name=f"Views {uuid.uuid4().hex[:8]}")
    session.add(ws)
    await session.flush()
    await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=paper.id))
    await evidence_cards.build_card(session, paper.id, llm=Scripted(card_reply(**reply)))
    return paper, ws


async def test_request_cards_with_no_papers_returns_nothing(session):
    assert await evidence_cards.request_cards(session, paper_ids=[]) == []


async def test_edit_changes_the_value_flags_it_and_marks_the_card_edited(session, tmp_path):
    paper, _ = await card_in_workspace(session, tmp_path)
    view = await evidence_cards.edit_card(
        session, paper.id, [{"field": "effect_sizes", "index": 0, "values": {"value": "d = 0.5"}}]
    )
    item = view.fields["effect_sizes"][0]
    assert (item["value"], item["flags"], view.provenance) == ("d = 0.5", ["edited"], "llm_edited")
    assert item["quote"] and item["page"] is not None  # the evidence is untouched
    again = await evidence_cards.get_card(session, paper.id)
    assert again.fields["effect_sizes"][0]["value"] == "d = 0.5"


async def test_edit_can_clear_a_value_and_keeps_the_quote(session, tmp_path):
    paper, _ = await card_in_workspace(session, tmp_path)
    view = await evidence_cards.edit_card(session, paper.id, [{"field": "design", "values": {"value": None}}])
    assert view.fields["design"]["value"] is None and view.fields["design"]["quote"]


async def test_edit_refuses_quotes_unknown_fields_and_items_without_evidence(session, tmp_path):
    paper, _ = await card_in_workspace(session, tmp_path)
    for bad in (
        {"field": "design", "values": {"quote": "x y z"}},
        {"field": "nope", "values": {"value": "a"}},
        {"field": "findings", "index": 9, "values": {"value": "a"}},
        {"field": "limitations", "index": 0, "values": {"value": "a"}},
    ):
        with pytest.raises(InvalidInput):
            await evidence_cards.edit_card(session, paper.id, [bad])
    assert (await evidence_cards.get_card(session, paper.id)).provenance == "llm"


async def test_edit_and_get_of_a_paper_without_a_card_are_not_found(session, tmp_path):
    paper = (await chunked_paper(session, tmp_path))[0]
    with pytest.raises(NotFound):
        await evidence_cards.get_card(session, paper.id)
    with pytest.raises(NotFound):
        await evidence_cards.edit_card(session, paper.id, [])


async def test_workspace_cards_list_every_paper_with_its_card_or_none(session, tmp_path):
    paper, ws = await card_in_workspace(session, tmp_path / "a")
    bare = (await chunked_paper(session, tmp_path / "b", title="No card"))[0]
    await session.execute(insert(workspace_papers).values(workspace_id=ws.id, paper_id=bare.id))
    rows = {r.paper_id: r for r in await evidence_cards.workspace_cards(session, ws.id)}
    assert rows[paper.id].first_author == "Ada Lovelace" and rows[paper.id].year == 2021
    assert rows[paper.id].card.status == "ready" and rows[bare.id].card is None
    with pytest.raises(NotFound):
        await evidence_cards.workspace_cards(session, uuid.uuid4())


async def test_csv_has_bom_header_one_row_per_kept_item_and_skips_dropped_ones(session, tmp_path):
    reply = {**FULL, "limitations": [{"value": "Invented", "quote": "nothing like this in the text"}]}
    paper, ws = await card_in_workspace(session, tmp_path, reply)
    change = {"field": "findings", "index": 0, "values": {"value": 'a, "b"'}}
    await evidence_cards.edit_card(session, paper.id, [change])
    text = await evidence_cards.workspace_csv(session, ws.id)
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0] == ["paper", "year", "field", "value", "quote", "page", "author", "edited"]
    fields = [r[2] for r in rows[1:]]
    assert "limitations" not in fields and fields.count("effect_sizes") == 1
    finding = next(r for r in rows if r[2] == "findings")
    assert finding[0] == "Recall study" and finding[1] == "2021" and finding[3] == 'a, "b"'
    assert finding[6] == "Ada Lovelace" and finding[7] == "yes"
    assert next(r for r in rows if r[2] == "design")[7] == "no"
    assert all(r[3] for r in rows[1:])  # never a row without a value


async def test_csv_neutralises_formula_cells(session, tmp_path):
    reply = {**FULL, "findings": [{"value": "x", "quote": "measured recall after one week"}]}
    paper, ws = await card_in_workspace(session, tmp_path, reply)
    paper.title = "=HYPERLINK(1)"
    await evidence_cards.edit_card(session, paper.id, [{"field": "findings", "index": 0, "values": {"value": "-5"}}])
    rows = list(csv.reader(io.StringIO((await evidence_cards.workspace_csv(session, ws.id)).lstrip("\ufeff"))))
    assert next(r for r in rows if r[2] == "findings")[3] == "'-5"
    assert all(c[:1] not in "=+-@\t\r" for r in rows[1:] for c in (r[0], r[3], r[4], r[6]) if c)
    assert rows[1][0] == "'=HYPERLINK(1)"
    assert evidence_cards._cell("=1+1") == "'=1+1" and evidence_cards._cell("") == ""


async def test_edit_rejects_bool_and_non_int_index_and_n(session, tmp_path):
    paper, _ = await card_in_workspace(session, tmp_path)
    for bad in (
        {"field": "findings", "index": True, "values": {"value": "z"}},
        {"field": "sample", "values": {"n": True}},
        {"field": "sample", "values": {"n": 12.5}},
        {"field": "sample", "values": {"n": "120"}},
    ):
        with pytest.raises(InvalidInput):
            await evidence_cards.edit_card(session, paper.id, [bad])


async def test_edit_of_a_card_that_is_not_ready_is_refused(session, tmp_path):
    paper, _ = await card_in_workspace(session, tmp_path)
    for status in ("generating", "failed"):
        card = await evidence_cards.get_card(session, paper.id)
        assert card
        row = await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper.id))
        row.status = status
        await session.flush()
        with pytest.raises(Conflict, match="card_not_ready"):
            await evidence_cards.edit_card(session, paper.id, [{"field": "design", "values": {"value": "z"}}])
