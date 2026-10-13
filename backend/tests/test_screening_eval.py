import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.workspace_search import WorkspaceSearchHit
from evals import screening_eval as ev
from tests.screening_pool import make_pool

pytestmark = pytest.mark.anyio

ABSTRACT = "we ran a randomised controlled trial of sleep deprivation in adults"
QUOTE = "randomised controlled trial of sleep deprivation"


def reply(decision, relevance=3, quotes=(QUOTE,), reason=None):
    return json.dumps({"relevance": relevance, "decision": decision, "reason": reason, "quotes": list(quotes),
                       "why": "because"})


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)

    async def stream(self, system, prompt):
        yield self.replies.pop(0)


# Decided newest first: the scripted replies below are in this order.
OWNER = ["relevant", "maybe", "not_relevant", "not_relevant", "relevant"]
REPLIES = [
    reply("include", 5),
    reply("unsure", 3),
    reply("exclude", 1, reason="wrong_topic"),
    reply("include", 4, quotes=("not in the abstract at all",)),
    reply("exclude", 2, reason="wrong_topic"),
]


def config(**over):
    return ev.Config(workspace="x", min_decided=3, missed_relevant_rate_max=0.5, quote_drop_rate_max=0.5, **over)


async def seed(session):
    rows = [(ABSTRACT, status) for status in reversed(OWNER)] + [(ABSTRACT, None)]
    workspace, hits = await make_pool(session, rows)
    workspace.research_question = "does sleep matter?"
    await session.flush()
    return workspace, hits


async def test_metrics_and_exit_code(session):
    workspace, hits = await seed(session)
    metrics, rows = await ev.evaluate(session, workspace.name, Scripted(REPLIES), None)
    assert metrics.decided == 5  # the undecided hit is skipped
    # agree: include/relevant, unsure/maybe, exclude/not_relevant; not: include/not_relevant, exclude/relevant
    assert metrics.agreement == 3 / 5
    assert metrics.missed_relevant_rate == 1 / 3  # one of relevant, maybe, relevant was excluded
    assert metrics.exclude_precision == 1 / 2
    assert metrics.unsure_share == 1 / 5
    assert metrics.quote_drop_rate == 1 / 5
    assert metrics.mean_relevance == {"relevant": 3.5, "maybe": 3.0, "not_relevant": 2.5}
    assert [row["label"] for row in rows] == OWNER
    assert ev.passes(metrics, config()) is True
    assert ev.passes(metrics, config().model_copy(update={"missed_relevant_rate_max": 0.1})) is False
    assert ev.passes(metrics, config().model_copy(update={"min_decided": 6})) is False


async def test_writes_nothing_and_limit_takes_most_recent(session):
    workspace, hits = await seed(session)
    before = {h.id: (h.stage1_status, h.stage1_decided_at, h.suggestion_model) for h in hits}
    metrics, rows = await ev.evaluate(session, workspace.name, Scripted(REPLIES[:2]), 2)
    assert metrics.decided == 2 and [row["label"] for row in rows] == OWNER[:2]
    await session.flush()
    after = (await session.execute(select(WorkspaceSearchHit).where(WorkspaceSearchHit.id.in_(before)))).scalars()
    assert {h.id: (h.stage1_status, h.stage1_decided_at, h.suggestion_model) for h in after} == before
    assert all(h.suggestion_model is None for h in hits)


async def test_execute_exit_codes_and_out(session, tmp_path):
    workspace, _ = await seed(session)
    out = tmp_path / "rows.jsonl"
    assert await ev.execute(session, config(), workspace.name, Scripted(REPLIES), None, out) == 0
    first = json.loads(out.read_text().splitlines()[0])
    assert {"title", "label", "verdict", "relevance", "confidence", "flags", "seconds"} <= set(first)
    strict = config().model_copy(update={"missed_relevant_rate_max": 0.1})
    assert await ev.execute(session, strict, workspace.name, Scripted(REPLIES), None, None) == 1
    assert await ev.execute(session, config(), "no such workspace", Scripted([]), None, None) == 2


def test_config_file_loads():
    cfg = ev.load_config(ev.CONFIG)
    assert (cfg.workspace, cfg.min_decided) == ("PaperLab", 30)
    with pytest.raises(ValidationError):
        ev.Config.model_validate({"workspace": "x"})
