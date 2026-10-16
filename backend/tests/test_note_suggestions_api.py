import uuid

import pytest
from test_chat import make_paper

from app.main import create_app

pytestmark = pytest.mark.anyio


def test_suggest_notes_route_takes_no_required_model_id():
    """Regression: resolve_llm_by_model_id's model_id started life without a `= None` default, which FastAPI
    treats as a REQUIRED query param despite the `| None` type annotation -- every real call to this route 404'd
    with "model_id: Field required" until a live check caught it (fake_llm overrides the whole dependency in
    every other test here, so none of them exercised the real query-param resolution)."""
    op = create_app().openapi()["paths"]["/api/papers/{paper_id}/notes/suggest"]["post"]
    [model_id_param] = [p for p in op["parameters"] if p["name"] == "model_id"]
    assert model_id_param["required"] is False


async def test_suggest_notes_route_returns_cards_promotable_through_the_existing_endpoint(client, session, fake_llm):
    """End-to-end: the generation endpoint's own output_id and a returned suggestion's chunk_id are enough,
    unmodified, to accept it through POST /api/notes/promote -- the same endpoint a chat answer's own selection
    already uses. No new "accept" endpoint exists or is needed."""
    paper = await make_paper(session, ["one", "two", "three"])

    resp = await client.post(f"/api/papers/{paper.id}/notes/suggest")

    assert resp.status_code == 200
    body = resp.json()
    assert [s["body"] for s in body["suggestions"]] == ["Fake first passage note.", "Fake third passage note."]
    first = body["suggestions"][0]
    assert {"chunk_id", "page", "section", "bbox"} <= first.keys()

    promoted = await client.post(
        "/api/notes/promote",
        json={"output_id": body["output_id"], "body": first["body"], "chunk_ids": [first["chunk_id"]]},
    )

    assert promoted.status_code == 201
    note = promoted.json()
    assert (note["provenance"], note["source_id"], note["body"]) == ("llm", body["output_id"], first["body"])


async def test_suggest_notes_does_not_appear_in_the_papers_chat_history(client, session, fake_llm):
    paper = await make_paper(session, ["one"])

    await client.post(f"/api/papers/{paper.id}/notes/suggest")
    history = await client.get(f"/api/papers/{paper.id}/chat")

    assert history.json() == []


async def test_a_model_failure_is_a_502_with_the_reason_not_a_bare_500(client, session, fake_llm):
    """Found live on 2026-10-16: a broken upstream behind an OpenAI-compatible proxy raised LLMError, which reached
    the reader as "Internal Server Error". The provider's message is meant to be shown (providers/base.py)."""
    paper = await make_paper(session, ["one", "two", "three"])
    fake_llm.fail_after = 0

    resp = await client.post(f"/api/papers/{paper.id}/notes/suggest")

    assert (resp.status_code, resp.json()) == (502, {"detail": "fake model failed mid-answer"})


async def test_suggest_notes_for_a_paper_not_ready_is_409(client, session, fake_llm):
    paper = await make_paper(session, ["one"], status="chunking")

    resp = await client.post(f"/api/papers/{paper.id}/notes/suggest")

    assert resp.status_code == 409


async def test_suggest_notes_for_unknown_paper_is_404(client, fake_llm):
    resp = await client.post(f"/api/papers/{uuid.uuid4()}/notes/suggest")

    assert resp.status_code == 404
