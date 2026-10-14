import json
import uuid

import pytest
from pdf_papers import chunked_paper
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core import retrieval
from app.core.errors import Conflict
from app.models import EvidenceCard, LLMOutput
from evals import evidence_cards_eval as ev

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

INTRO = (
    "We recruited 120 participants from three universities and measured recall after one week. "
    "The treatment effect was medium, d = 0.45, 95% CI [0.21, 0.69], p < .001, and the change was -0.30 points."
)
Q_N = "We recruited 120 participants from three universities"
Q_EFFECT = "d = 0.45, 95% CI [0.21, 0.69], p < .001"
Q_TREND = "The treatment effect was medium"
Q_DESIGN = "measured recall after one week"


class Scripted:
    model, connection_name = "fake:scripted", "Test"

    def __init__(self, *replies):
        self.replies = list(replies)

    async def stream(self, system, prompt):
        yield self.replies.pop(0)


def reply(**over):
    parts = {
        "research_question": {"value": "Does the treatment improve recall?", "quote": Q_TREND},
        "design": {"value": "Recall measured after one week", "quote": Q_DESIGN},
        "sample": {"n": 120, "population": "university participants", "quote": Q_N},
        "findings": [],
        "effect_sizes": [{"value": "d = 0.45", "ci": "[0.21, 0.69]", "p": "< .001", "quote": Q_EFFECT}],
        "limitations": [{"value": "Only three universities", "quote": "from three universities"}],
    }
    return json.dumps({**parts, **over})


@pytest.fixture(autouse=True)
def search_with_the_test_embedder(embedder, monkeypatch):
    async def query_embedder(session, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)


def case(prefix, **over):
    base = {
        "paper": prefix, "n": 120, "effect_sizes": ["d = 0.45"], "research_question": ["treatment", "recall"],
        "design": ["recall", "week"], "limitation": ["three", "universities"],
    }
    return ev.Case(**{**base, **over})


async def seed(session, tmp_path):
    prefix = f"Cards eval {uuid.uuid4().hex}"
    paper, _, _ = await chunked_paper(session, tmp_path, title=f"{prefix} paper", intro=INTRO)
    return paper, prefix


def config(cases, **over):
    return ev.Config(gates=ev.Gates(field_hit_min=0.6, n_exact_min=0.6, **over), cases=cases)


async def test_a_card_that_matches_hits_every_field_and_nothing_is_written(session, tmp_path):
    paper, prefix = await seed(session, tmp_path)
    cards = await session.scalar(select(func.count()).select_from(EvidenceCard))
    outputs = await session.scalar(select(func.count()).select_from(LLMOutput))
    metrics, rows = await ev.evaluate(session, [case(prefix)], Scripted(reply()), None)
    assert rows[0]["hits"] == dict.fromkeys(["n", "effect_sizes", "research_question", "design", "limitation"], True)
    assert metrics.field_hit == 1.0 and metrics.n_exact == 1.0 and metrics.cards == 1
    assert metrics.quote_kept_rate == 1.0 and metrics.number_drops == 0 and metrics.unsupported_rate == 0.0
    assert metrics.median_seconds >= 0
    assert await session.scalar(select(func.count()).select_from(EvidenceCard)) == cards
    assert await session.scalar(select(func.count()).select_from(LLMOutput)) == outputs


async def test_misses_dropped_items_and_the_null_n_are_scored(session, tmp_path):
    paper, prefix = await seed(session, tmp_path)
    bad = reply(
        sample={"n": 120, "quote": "We recruited 999 participants"},  # invented quote: unsupported, n dropped
        effect_sizes=[{"value": "d = 0.54", "quote": Q_EFFECT}],  # 0.54 is not in its quote: number_not_in_source
        design={"value": "A survey", "quote": Q_DESIGN},  # kept, but no expected word in it
    )
    metrics, rows = await ev.evaluate(session, [case(prefix), case(prefix, n=None)], Scripted(bad, bad), None)
    first, second = rows
    assert first["hits"] == {
        "n": False, "effect_sizes": False, "research_question": True, "design": False, "limitation": True,
    }
    assert second["hits"]["n"] is True  # the paper states none and the card shows none
    assert metrics.n_exact == 0.5 and metrics.number_drops == 2 and metrics.unsupported_rate == 2 / 10
    assert metrics.quote_kept_rate == 6 / 10
    assert metrics.per_field["effect_sizes"] == 0.0 and metrics.per_field["research_question"] == 1.0


async def test_a_case_without_a_limitation_does_not_score_that_field(session, tmp_path):
    paper, prefix = await seed(session, tmp_path)
    _, rows = await ev.evaluate(session, [case(prefix, limitation=[])], Scripted(reply()), None)
    assert "limitation" not in rows[0]["hits"]


async def test_key_words_need_half_to_hit(session, tmp_path):
    paper, prefix = await seed(session, tmp_path)
    c = case(prefix, research_question=["treatment", "zzz", "yyy"])
    _, rows = await ev.evaluate(session, [c, c], Scripted(reply(), reply()), None)
    assert rows[0]["hits"]["research_question"] is False  # 1 of 3
    c2 = case(prefix, research_question=["treatment", "zzz"])
    _, rows = await ev.evaluate(session, [c2], Scripted(reply()), None)
    assert rows[0]["hits"]["research_question"] is True  # 1 of 2


async def test_gates_and_limit(session, tmp_path):
    paper, prefix = await seed(session, tmp_path)
    metrics, rows = await ev.evaluate(session, [case(prefix)] * 3, Scripted(reply()), 1)
    assert metrics.cases == 1 and len(rows) == 1
    assert ev.passes(metrics, ev.Gates(field_hit_min=1, n_exact_min=1)) is True
    worse, _ = await ev.evaluate(session, [case(prefix, n=7)], Scripted(reply()), None)
    assert ev.passes(worse, ev.Gates(field_hit_min=0.6, n_exact_min=0.6)) is False


async def test_a_model_that_never_answers_fails_the_run(session, tmp_path, capsys):
    paper, prefix = await seed(session, tmp_path)
    assert await ev.execute(session, config([case(prefix)]), Scripted("nope", "nope"), None, None) == 1
    assert "no card was built" in capsys.readouterr().out


async def test_unknown_paper_is_bad_input(session, capsys):
    cases = [case("No such paper " + uuid.uuid4().hex)]
    assert await ev.execute(session, config(cases), Scripted(), None, None) == 2
    assert "matches 0 papers" in capsys.readouterr().err


async def test_execute_exit_codes_and_out(session, tmp_path, capsys):
    paper, prefix = await seed(session, tmp_path)
    out = tmp_path / "rows.jsonl"
    assert await ev.execute(session, config([case(prefix)]), Scripted(reply()), None, out) == 0
    first = json.loads(out.read_text().splitlines()[0])
    assert {"paper", "status", "hits", "fields", "seconds"} <= set(first)
    assert "PASS" in capsys.readouterr().out
    strict = ev.Config(gates=ev.Gates(field_hit_min=0.6, n_exact_min=0.6), cases=[case(prefix, n=5)])
    assert await ev.execute(session, strict, Scripted(reply()), None, None) == 1
    assert "N exact 0.00 < 0.6" in capsys.readouterr().out


def test_config_file_loads_with_ten_real_cases():
    cfg = ev.load_config(ev.CONFIG)
    assert (cfg.gates.field_hit_min, cfg.gates.n_exact_min) == (0.6, 0.6)
    assert len(cfg.cases) == 10 and len({c.paper for c in cfg.cases}) == 10
    with pytest.raises(ValidationError):
        ev.Case.model_validate({"paper": "x", "n": 1, "effect_sizes": [], "research_question": ["a"], "design": ["a"],
                                "limitation": []})
    with pytest.raises(ValidationError):
        ev.Config.model_validate({"gates": {"field_hit_min": 1, "n_exact_min": 1}, "cases": [], "x": 1})


def run_main(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["evidence_cards_eval", *args])
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


def test_key_words_match_by_a_tiny_stem():
    assert ev._words_hit(["creative", "memory"], "creativity ratings")
    assert not ev._words_hit(["creative", "memory"], "originality ratings")
    assert ev._words_hit(["measures"], "we measure it")
