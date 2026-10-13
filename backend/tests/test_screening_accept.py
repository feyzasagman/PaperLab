"""Accept (the owner's action on AI suggestions) and the AI-score ordering of the hit list (M39 F3)."""

import uuid

import pytest

from app.core import ai_sort, screening, workspace_search
from app.core.errors import InvalidInput
from tests.screening_pool import make_pool

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


def _suggest(hit, verdict, relevance=None, confidence="high", reason=None):
    hit.suggestion, hit.suggestion_relevance = verdict, relevance
    hit.suggestion_confidence, hit.suggestion_reason = confidence, reason


async def test_accept_maps_include_and_exclude_and_skips_the_rest(session):
    workspace, hits = await make_pool(session, [("a", None)] * 6)
    inc, exc, unsure, none, decided, other = hits
    _suggest(inc, "include")
    _suggest(exc, "exclude", reason="wrong_topic")
    _suggest(unsure, "unsure")
    _suggest(decided, "include")
    decided.stage1_status = "not_relevant"
    decided.stage1_exclude_reason = "other"
    _, (foreign, *_) = await make_pool(session, [("z", None)])
    _suggest(foreign, "include")
    await session.flush()
    unknown = uuid.uuid4()

    result = await screening.accept_suggestions(
        session, workspace.id, [inc.id, exc.id, unsure.id, none.id, decided.id, foreign.id, unknown]
    )

    assert result.accepted == 2
    assert set(result.skipped) == {unsure.id, none.id, decided.id, foreign.id, unknown}
    assert (inc.stage1_status, inc.stage1_decided_at is not None) == ("relevant", True)
    assert (exc.stage1_status, exc.stage1_exclude_reason) == ("not_relevant", "wrong_topic")
    assert exc.stage1_decided_at is not None
    assert unsure.stage1_status is None and foreign.stage1_status is None
    assert (decided.stage1_status, decided.stage1_exclude_reason) == ("not_relevant", "other")


async def _ai_pool(session):
    workspace, hits = await make_pool(session, [("a", None)] * 6)
    a, b, c, d, e, f = hits  # found order a..f
    _suggest(a, "include", 3, "high")
    _suggest(b, "include", 5, "low")
    _suggest(c, "include", 5, "high")
    _suggest(d, "exclude", 5, "high", "other")  # ties c on both keys: found order breaks it
    _suggest(e, "include", 4, "low")
    # f has no suggestion: last
    await session.flush()
    return workspace, hits


async def test_ai_sort_orders_by_relevance_then_confidence_then_found_order(session):
    workspace, (a, b, c, d, e, f) = await _ai_pool(session)
    items, _ = await workspace_search.list_hits(session, workspace.id, sort="ai")
    assert [i["id"] for i in items] == [c.id, d.id, b.id, e.id, a.id, f.id]


async def test_min_relevance_keeps_only_that_and_above(session):
    workspace, (a, b, c, d, e, f) = await _ai_pool(session)
    items, _ = await workspace_search.list_hits(session, workspace.id, sort="ai", min_relevance=4)
    assert {i["id"] for i in items} == {b.id, c.id, d.id, e.id}


async def test_ai_sort_pages_with_an_offset_cursor_without_repeats(session):
    workspace, hits = await _ai_pool(session)
    seen, after = [], None
    while True:
        items, after = await workspace_search.list_hits(session, workspace.id, limit=2, after=after, sort="ai")
        seen += [i["id"] for i in items]
        if after is None:
            break
    full, _ = await workspace_search.list_hits(session, workspace.id, sort="ai")
    assert seen == [i["id"] for i in full] and len(set(seen)) == 6


async def test_ai_sort_rejects_a_bad_cursor_and_unknown_sort(session):
    workspace, _ = await _ai_pool(session)
    with pytest.raises(InvalidInput):
        await workspace_search.list_hits(session, workspace.id, after="!!!", sort="ai")
    with pytest.raises(InvalidInput):
        await workspace_search.list_hits(session, workspace.id, sort="bogus")


async def test_accept_skips_an_exclude_with_no_reason(session):
    workspace, (hit,) = await make_pool(session, [("a", None)])
    _suggest(hit, "exclude", reason=None)
    await session.flush()
    result = await screening.accept_suggestions(session, workspace.id, [hit.id])
    assert (result.accepted, result.skipped, hit.stage1_status) == (0, [hit.id], None)


@pytest.mark.parametrize("cursor", [ai_sort.encode_offset(-1), ai_sort.encode_offset(ai_sort.MAX_OFFSET + 1)])
async def test_ai_sort_rejects_a_negative_or_oversized_offset(session, cursor):
    workspace, _ = await _ai_pool(session)
    with pytest.raises(InvalidInput):
        await workspace_search.list_hits(session, workspace.id, after=cursor, sort="ai")


@pytest.mark.parametrize("bad", [0, 6, -1])
async def test_min_relevance_must_be_1_to_5(session, bad):
    workspace, _ = await _ai_pool(session)
    with pytest.raises(InvalidInput):
        await workspace_search.list_hits(session, workspace.id, min_relevance=bad)
