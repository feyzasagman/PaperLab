import uuid

import pytest

from tests.screening_pool import make_pool

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


def _base(workspace):
    return f"/api/workspaces/{workspace.id}/search/hits"


async def test_accept_writes_decisions_and_reports_skipped(client, session):
    workspace, hits = await make_pool(session, [("a", None), ("b", None), ("c", None), ("d", "relevant")])
    inc, exc, unsure, decided = hits
    inc.suggestion = "include"
    exc.suggestion, exc.suggestion_reason = "exclude", "wrong_topic"
    unsure.suggestion = "unsure"
    decided.suggestion = "include"
    await session.flush()
    ids = [str(h.id) for h in hits] + [str(uuid.uuid4())]
    resp = await client.post(f"{_base(workspace)}/accept-suggestions", json={"hit_ids": ids})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 2
    assert set(body["skipped"]) == {str(unsure.id), str(decided.id), ids[-1]}
    for hit in hits:
        await session.refresh(hit)
    assert (inc.stage1_status, exc.stage1_status) == ("relevant", "not_relevant")
    assert exc.stage1_exclude_reason == "wrong_topic"
    assert unsure.stage1_status is None


async def test_accept_needs_a_list_of_uuids(client, session):
    workspace, _ = await make_pool(session, [])
    assert (await client.post(f"{_base(workspace)}/accept-suggestions", json={"hit_ids": ["x"]})).status_code == 422
    assert (await client.post(f"{_base(workspace)}/accept-suggestions", json={})).status_code == 422


async def test_hits_return_the_five_suggestion_fields(client, session):
    workspace, [hit] = await make_pool(session, [("a", None)])
    hit.suggestion_relevance, hit.suggestion_quotes = 4, ["q"]
    hit.suggestion_confidence, hit.suggestion_flags, hit.suggestion_prompt_version = "high", ["f"], 2
    await session.flush()
    item = (await client.get(_base(workspace))).json()["items"][0]
    assert (item["suggestion_relevance"], item["suggestion_quotes"], item["suggestion_confidence"],
            item["suggestion_flags"], item["suggestion_prompt_version"]) == (4, ["q"], "high", ["f"], 2)


async def test_hits_unchanged_without_the_new_params(client, session):
    workspace, hits = await make_pool(session, [("a", None), ("b", None)])
    items = (await client.get(_base(workspace))).json()["items"]
    assert [i["id"] for i in items] == [str(h.id) for h in hits]
    assert items[0]["suggestion_relevance"] is None


async def test_sort_ai_and_min_relevance(client, session):
    workspace, hits = await make_pool(session, [("a", None), ("b", None), ("c", None)])
    for hit, score in zip(hits, (2, 5, 4)):
        hit.suggestion_relevance = score
    await session.flush()
    ranked = (await client.get(_base(workspace), params={"sort": "ai"})).json()["items"]
    assert [i["suggestion_relevance"] for i in ranked] == [5, 4, 2]
    kept = (await client.get(_base(workspace), params={"min_relevance": 4})).json()["items"]
    assert sorted(i["suggestion_relevance"] for i in kept) == [4, 5]


@pytest.mark.parametrize("params", [{"min_relevance": 0}, {"min_relevance": 6}, {"sort": "bogus"}])
async def test_bad_sort_or_min_relevance_is_422(client, session, params):
    workspace, _ = await make_pool(session, [])
    assert (await client.get(_base(workspace), params=params)).status_code == 422


async def test_accept_caps_the_id_list(client, session):
    workspace, _ = await make_pool(session, [])
    ids = [str(uuid.uuid4()) for _ in range(5001)]
    assert (await client.post(f"{_base(workspace)}/accept-suggestions", json={"hit_ids": ids})).status_code == 422
