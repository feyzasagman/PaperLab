import json
import uuid

import pytest
from pdf_papers import TWO_LINE_QUOTE, chunked_paper
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core import retrieval
from app.core.errors import Conflict
from app.models import LLMOutput, Note
from app.providers.base import LLMError
from evals import claims_eval as ev

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]


class Scripted:
    model, connection_name = "fake:scripted", "Test"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.systems = []

    async def stream(self, system, prompt):
        self.systems.append(system)
        yield self.replies.pop(0)


def reply(verdict, quote=""):
    return json.dumps({"verdict": verdict, "passage": None, "quote": quote, "reason": "because"})


@pytest.fixture(autouse=True)
def search_with_the_test_embedder(embedder, monkeypatch):
    async def query_embedder(session, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)


KINDS = [
    ("true", ["supported"]), ("overstated", ["partially_supported", "not_supported"]),
    ("contradicted", ["not_supported"]), ("unrelated", ["not_addressed"]),
]


async def seed(session, tmp_path):
    prefix = f"Claims eval {uuid.uuid4().hex}"
    paper, _, _ = await chunked_paper(session, tmp_path, title=f"{prefix} paper")
    cases = [ev.Case(paper=prefix, claim=f"claim {kind}", kind=kind, accepted=acc) for kind, acc in KINDS]
    return paper, cases


def config(cases, **over):
    return ev.Config(gates=ev.Gates(accuracy_min=0.7, overstated_min=0.6, **over), cases=cases)


# Scripted in case order. The contradicted claim gets "supported": the one wrong answer.
REPLIES = [
    reply("supported", TWO_LINE_QUOTE), reply("partially_supported", "not in the paper at all"),
    reply("supported", TWO_LINE_QUOTE), reply("not_addressed"),
]


async def test_metrics_and_gates(session, tmp_path):
    paper, cases = await seed(session, tmp_path)
    notes_before = await session.scalar(select(func.count()).select_from(Note))
    metrics, rows = await ev.evaluate(session, cases, Scripted(*REPLIES), None)
    assert metrics.cases == 4 and metrics.accuracy == 3 / 4
    assert metrics.per_kind == {"true": 1.0, "overstated": 1.0, "contradicted": 0.0, "unrelated": 1.0}
    assert metrics.overstated_accuracy == 1.0
    assert metrics.confusion["contradicted"] == {"supported": 1}
    # three supported-or-partial verdicts; the overstated one's quote was not in the paper and is dropped
    assert metrics.quote_kept_rate == 2 / 3
    assert metrics.cannot_verify == 0 and metrics.median_seconds >= 0
    assert [r["kind"] for r in rows] == [k for k, _ in KINDS]
    assert await session.scalar(select(func.count()).select_from(Note)) == notes_before  # never saves
    assert ev.passes(metrics, config(cases).gates) is True
    assert ev.passes(metrics, ev.Gates(accuracy_min=0.8, overstated_min=0.6)) is False


async def test_limit_and_cannot_verify_count(session, tmp_path):
    paper, cases = await seed(session, tmp_path)
    bad = Scripted("nope", "nope")  # two invalid replies: cannot_verify / model_error
    metrics, rows = await ev.evaluate(session, cases, bad, 1)
    assert metrics.cases == 1 and metrics.cannot_verify == 1 and metrics.accuracy == 0.0
    assert metrics.quote_kept_rate is None


async def test_unknown_paper_is_bad_input(session, tmp_path, capsys):
    cases = [ev.Case(paper="No such paper " + uuid.uuid4().hex, claim="c", kind="true", accepted=["supported"])]
    assert await ev.execute(session, config(cases), Scripted(), None, None) == 2
    assert "matches 0 papers" in capsys.readouterr().err


async def test_execute_exit_codes_and_out(session, tmp_path, capsys):
    paper, cases = await seed(session, tmp_path)
    out = tmp_path / "rows.jsonl"
    assert await ev.execute(session, config(cases), Scripted(*REPLIES), None, out) == 0
    first = json.loads(out.read_text().splitlines()[0])
    assert {"claim", "kind", "verdict", "correct", "quote", "seconds"} <= set(first)
    capsys.readouterr()
    strict = config(cases).model_copy(update={"gates": ev.Gates(accuracy_min=0.9, overstated_min=0.6)})
    assert await ev.execute(session, strict, Scripted(*REPLIES), None, None) == 1
    assert "accuracy 0.75 < 0.9" in capsys.readouterr().out
    assert await ev.execute(session, config(cases), Scripted(*(["nope"] * 8)), None, None) == 1


# papers added from the research pilot carry one true case each
PILOT_PAPERS = {"Art or Artifice?", "Why the standard definition of creativity"}


def test_config_file_loads_with_enough_cases():
    cfg = ev.load_config(ev.CONFIG)
    assert (cfg.gates.accuracy_min, cfg.gates.overstated_min) == (0.7, 0.6)
    assert len(cfg.cases) >= 26 and len({c.paper for c in cfg.cases}) >= 6
    for paper in {c.paper for c in cfg.cases} - PILOT_PAPERS:
        assert {c.kind for c in cfg.cases if c.paper == paper} == {"true", "overstated", "contradicted", "unrelated"}
    with pytest.raises(ValidationError):
        ev.Case.model_validate({"paper": "x", "claim": "c", "kind": "true", "accepted": ["maybe"]})
    with pytest.raises(ValidationError):
        ev.Config.model_validate({"gates": {"accuracy_min": 1, "overstated_min": 1}, "cases": [], "x": 1})


def run_main(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["claims_eval", *args])
    with pytest.raises(SystemExit) as exit_info:
        ev.main()
    return exit_info.value.code


def test_cli_bad_input_exits_2(monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases: []\nextra: 1\n")
    assert run_main(monkeypatch, "--config", str(bad)) == 2
    assert run_main(monkeypatch, "--config", str(tmp_path / "missing.yaml")) == 2
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


async def test_overstated_gate_fails_on_its_own(session, tmp_path, capsys):
    paper, cases = await seed(session, tmp_path)
    # everything right except the overstated claim, which the model accepts outright: accuracy 0.75, overstated 0
    replies = [REPLIES[0], reply("supported", TWO_LINE_QUOTE), reply("not_supported"), REPLIES[3]]
    gates = ev.Gates(accuracy_min=0.7, overstated_min=0.6)
    assert await ev.execute(session, ev.Config(gates=gates, cases=cases), Scripted(*replies), None, None) == 1
    out = capsys.readouterr().out
    assert "overstated accuracy 0.00 < 0.6" in out and "accuracy 0.75" not in out


async def test_no_overstated_case_is_not_applicable_rather_than_a_failure(session, tmp_path, capsys):
    paper, cases = await seed(session, tmp_path)
    assert await ev.execute(session, config(cases), Scripted(REPLIES[0]), 1, None) == 0
    assert "overstated accuracy: n/a" in capsys.readouterr().out


async def test_the_eval_leaves_no_claim_check_rows_behind(session, tmp_path):
    paper, cases = await seed(session, tmp_path)
    before = await session.scalar(select(func.count()).select_from(LLMOutput))
    await ev.evaluate(session, cases, Scripted(*REPLIES), None)
    assert await session.scalar(select(func.count()).select_from(LLMOutput)) == before


async def test_a_failing_case_is_recorded_and_the_rest_still_run(session, tmp_path):
    paper, cases = await seed(session, tmp_path)

    class Flaky(Scripted):
        calls = 0

        async def stream(self, system, prompt):
            Flaky.calls += 1
            if Flaky.calls == 2:
                raise LLMError("boom")
            async for part in super().stream(system, prompt):
                yield part

    metrics, rows = await ev.evaluate(session, cases, Flaky(REPLIES[0], REPLIES[2], REPLIES[3]), None)
    assert [r["verdict"] for r in rows][1] == "check_failed" and len(rows) == 4
    assert metrics.cannot_verify == 1 and rows[1]["correct"] is False


async def test_rows_record_evidence_level_and_confidence(session, tmp_path):
    paper, cases = await seed(session, tmp_path)
    _, rows = await ev.evaluate(session, cases, Scripted(*REPLIES), None)
    assert rows[0]["evidence_level"] == "full_text" and rows[0]["confidence"] == "high"


async def test_prompt_version_1_runs_the_old_prompt_and_restores_v2(session, tmp_path, capsys):
    paper, cases = await seed(session, tmp_path)
    v2_system = ev.claims.SYSTEM_PROMPT
    llm = Scripted(*REPLIES)
    out = tmp_path / "rows.jsonl"
    assert await ev.execute(session, config(cases), llm, None, out, prompt_version=1) == 0
    assert all("continues a sentence" not in s for s in llm.systems) and llm.systems
    assert {json.loads(line)["prompt_version"] for line in out.read_text().splitlines()} == {1}
    assert "prompt_version=1" in capsys.readouterr().out
    assert ev.claims.SYSTEM_PROMPT == v2_system and ev.claims.CLAIM_PROMPT_VERSION == 2


async def test_an_unknown_prompt_version_is_bad_input(session, tmp_path, capsys):
    paper, cases = await seed(session, tmp_path)
    assert await ev.execute(session, config(cases), Scripted(), None, None, prompt_version=99) == 2
    assert "prompt version 99" in capsys.readouterr().err
    assert ev.claims.CLAIM_PROMPT_VERSION == 2


def test_cli_rejects_a_non_numeric_prompt_version(monkeypatch):
    assert run_main(monkeypatch, "--prompt-version", "x") == 2
