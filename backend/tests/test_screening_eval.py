import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.errors import Conflict
from app.models.workspace_search import WorkspaceSearchHit
from evals import screening_eval as ev
from tests.screening_pool import make_pool

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

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


def run_main(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["screening_eval", *args])
    with pytest.raises(SystemExit) as exit_info:
        ev.main()
    return exit_info.value.code


def test_cli_bad_config_exits_2(monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("workspace: x\ntypo_key: 1\n")
    assert run_main(monkeypatch, "--config", str(bad)) == 2
    assert run_main(monkeypatch, "--config", str(tmp_path / "missing.yaml")) == 2


def test_cli_rejects_bad_limit(monkeypatch):
    assert run_main(monkeypatch, "--limit", "0") == 2


def test_cli_no_default_model_exits_2(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def no_model(session, model_id):
        raise Conflict("no_model")

    monkeypatch.setattr(ev, "SessionLocal", FakeSession)
    monkeypatch.setattr(ev.llm_connections, "resolve", no_model)
    assert run_main(monkeypatch) == 2


async def test_no_relevant_hits_fails_with_reason(session, capsys):
    workspace, _ = await make_pool(session, [(ABSTRACT, "not_relevant")] * 3)
    workspace.research_question = "q"
    await session.flush()
    replies = [reply("exclude", 1, reason="wrong_topic")] * 3
    metrics, _ = await ev.evaluate(session, workspace.name, Scripted(replies), None)
    assert metrics.missed_relevant_rate is None and ev.passes(metrics, config()) is False
    code = await ev.execute(session, config(), workspace.name, Scripted(replies), None, None)
    assert code == 1 and "no relevant hits to measure misses against" in capsys.readouterr().out


async def test_a_decision_with_no_decided_at_still_counts_and_sorts_last(session):
    workspace, hits = await seed(session)
    old = hits[0]  # the oldest decision, made before decisions were timed
    assert old.stage1_status is not None
    old.stage1_decided_at = None
    await session.flush()
    metrics, rows = await ev.evaluate(session, workspace.name, Scripted(REPLIES), None)
    assert metrics.decided == 5 and rows[-1]["title"] == "paper 0"
    _, limited = await ev.evaluate(session, workspace.name, Scripted(REPLIES[:4]), 4)
    assert "paper 0" not in [row["title"] for row in limited]


async def test_only_abstract_hits_are_evaluated(session):
    rows = [(ABSTRACT, "relevant"), (None, "relevant"), ("  ", "not_relevant"), (ABSTRACT, "not_relevant")]
    workspace, _ = await make_pool(session, rows)
    workspace.research_question = "q"
    await session.flush()
    replies = [reply("exclude", 1, reason="wrong_topic"), reply("include", 5)]
    metrics, out = await ev.evaluate(session, workspace.name, Scripted(replies), None)
    assert (metrics.decided, metrics.skipped_no_abstract) == (2, 2)
    assert [r["label"] for r in out] == ["not_relevant", "relevant"]
    # --limit counts abstract hits only: the newest one alone
    workspace2, _ = await make_pool(session, rows)
    workspace2.research_question = "q"
    await session.flush()
    metrics, out = await ev.evaluate(session, workspace2.name, Scripted(replies[:1]), 1)
    assert (metrics.decided, metrics.skipped_no_abstract) == (1, 2)


async def test_all_model_errors_fail(session, capsys):
    workspace, _ = await make_pool(session, [(ABSTRACT, "relevant")] * 3)
    workspace.research_question = "q"
    await session.flush()
    code = await ev.execute(session, config(), workspace.name, Scripted(["garbage"] * 6), None, None)
    assert code == 1 and "no real model answer" in capsys.readouterr().out
    empty, _ = await make_pool(session, [(None, "relevant")])
    empty.research_question = "q"
    await session.flush()
    assert await ev.execute(session, config(), empty.name, Scripted([]), None, None) == 1


async def test_fail_line_names_only_failed_gates(session, capsys):
    workspace, _ = await seed(session)
    strict = config().model_copy(update={"missed_relevant_rate_max": 0.1})
    assert await ev.execute(session, strict, workspace.name, Scripted(REPLIES), None, None) == 1
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("FAIL"))
    assert "missed-relevant 0.33 > 0.1" in line
    assert "decided" not in line and "quote-drop" not in line
