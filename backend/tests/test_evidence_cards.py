import json

import pytest
from pdf_papers import chunked_paper
from sqlalchemy import func, select

from app.core import evidence_cards, retrieval
from app.models import EvidenceCard, LLMOutput, Paper, PaperStatus

pytestmark = [pytest.mark.anyio, pytest.mark.xdist_group("references")]

INTRO = (
    "We recruited 120 participants from three universities and measured recall after one week. "
    "The treatment effect was medium, d = 0.45, 95% CI [0.21, 0.69], p < .001, and the change was -0.30 points."
)
Q_N = "We recruited 120 participants from three universities"
Q_EFFECT = "d = 0.45, 95% CI [0.21, 0.69], p < .001"
Q_MINUS = "the change was -0.30 points"
Q_TREND = "The treatment effect was medium"


class Scripted:
    model, connection_name = "fake:scripted", "Test"

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    async def stream(self, system, prompt):
        self.prompts.append(prompt)
        yield self.replies.pop(0)


def item(value, quote, passage=None):
    return {"value": value, "passage": passage, "quote": quote}


def card_reply(**parts):
    return json.dumps(parts)


FULL = {
    "research_question": item("Does the treatment improve recall?", Q_TREND),
    "design": item("Participants were recruited and recall measured after a week.", "measured recall after one week"),
    "sample": {"n": 120, "population": "university participants", "quote": Q_N},
    "findings": [item("A medium treatment effect.", Q_TREND)],
    "effect_sizes": [
        {"value": "d = 0.45", "statistic": "Cohen's d", "ci": "[0.21, 0.69]", "p": "< .001", "quote": Q_EFFECT}
    ],
    "limitations": [],
}


@pytest.fixture(autouse=True)
def search_with_the_test_embedder(embedder, monkeypatch):
    async def query_embedder(session, given=None):
        return embedder

    monkeypatch.setattr(retrieval, "query_embedder", query_embedder)


@pytest.fixture
async def paper(session, tmp_path):
    paper, _, _ = await chunked_paper(session, tmp_path, intro=INTRO)
    return paper


async def build(session, paper, reply, **kw):
    llm = Scripted(reply) if isinstance(reply, str) else reply
    return await evidence_cards.build_card(session, paper.id, llm=llm, **kw), llm


async def test_a_full_card_keeps_every_backed_item_with_page_and_chunk(session, paper):
    view, llm = await build(session, paper, card_reply(**FULL))
    f = view.fields
    assert view.status == "ready" and view.error is None and view.provenance == "llm"
    assert f["research_question"]["value"] == "Does the treatment improve recall?"
    assert f["research_question"]["page"] == 1 and f["research_question"]["chunk_id"] is not None
    assert f["research_question"]["flags"] == []
    assert (f["sample"]["n"], f["sample"]["quote"]) == (120, Q_N)
    assert f["effect_sizes"][0]["ci"] == "[0.21, 0.69]" and f["effect_sizes"][0]["flags"] == []
    assert len(f["findings"]) == 1 and f["limitations"] == []
    assert "(page 1, section 1 Introduction)" in llm.prompts[0]


async def test_an_effect_size_whose_number_is_not_in_its_quote_is_dropped_and_flagged(session, paper):
    bad = {**FULL["effect_sizes"][0], "value": "d = 0.54"}
    view, _ = await build(session, paper, card_reply(**{**FULL, "effect_sizes": [bad, FULL["effect_sizes"][0]]}))
    values = [e["value"] for e in view.fields["effect_sizes"]]
    assert values == [None, "d = 0.45"]
    assert view.fields["effect_sizes"][0]["flags"] == ["number_not_in_source"]
    assert view.fields["effect_sizes"][0]["quote"] is None


async def test_a_sample_n_missing_from_its_quote_is_dropped(session, paper):
    view, _ = await build(session, paper, card_reply(**{**FULL, "sample": {**FULL["sample"], "n": 112}}))
    assert view.fields["sample"]["n"] is None and view.fields["sample"]["flags"] == ["number_not_in_source"]


async def test_leading_zero_and_unicode_minus_do_not_cause_a_mismatch(session, paper):
    effect = {"value": "p = 0.001", "p": "0.001", "quote": Q_EFFECT}
    minus = {"value": "\u22120.30 points", "quote": Q_MINUS}  # the PDF font has no U+2212 glyph
    view, _ = await build(session, paper, card_reply(**{**FULL, "effect_sizes": [effect, minus]}))
    assert [e["value"] for e in view.fields["effect_sizes"]] == ["p = 0.001", "\u22120.30 points"]
    assert all(e["flags"] == [] for e in view.fields["effect_sizes"])


async def test_a_text_value_with_a_number_not_in_its_quote_is_dropped(session, paper):
    wrong = item("The effect was 45% larger.", Q_TREND)
    view, _ = await build(session, paper, card_reply(**{**FULL, "findings": [wrong]}))
    assert view.fields["findings"][0]["value"] is None
    assert view.fields["findings"][0]["flags"] == ["number_not_in_source"]


async def test_an_invented_quote_is_unsupported_and_its_value_never_shown(session, paper):
    invented = item("Recall improved a lot.", "a sentence the paper never wrote")
    view, _ = await build(session, paper, card_reply(**{**FULL, "research_question": invented, "findings": [invented]}))
    assert view.fields["research_question"] == {
        "value": None, "quote": None, "page": None, "chunk_id": None, "flags": ["unsupported"],
    }
    assert view.fields["findings"][0]["value"] is None and view.fields["findings"][0]["flags"] == ["unsupported"]


async def test_a_quote_naming_the_wrong_passage_is_kept_with_the_right_page(session, paper):
    view, _ = await build(session, paper, card_reply(**{**FULL, "research_question": item("RQ", Q_TREND, 99)}))
    assert view.fields["research_question"]["value"] == "RQ" and view.fields["research_question"]["page"] == 1


async def test_lists_are_capped(session, paper):
    many = [item(f"Finding {i}", Q_TREND) for i in range(9)]
    view, _ = await build(session, paper, card_reply(**{**FULL, "findings": many, "limitations": many}))
    assert len(view.fields["findings"]) == 5 and len(view.fields["limitations"]) == 5


async def test_an_empty_field_is_not_flagged(session, paper):
    view, _ = await build(session, paper, card_reply(**{**FULL, "design": None}))
    assert view.fields["design"]["value"] is None and view.fields["design"]["flags"] == []


async def test_a_paper_that_is_not_ready_fails_without_asking_the_model(session):
    paper = Paper(title="Busy", file_path="x.pdf", status=PaperStatus.EXTRACTING)
    session.add(paper)
    await session.flush()
    llm = Scripted()
    view = await evidence_cards.build_card(session, paper.id, llm=llm)
    assert (view.status, view.error) == ("failed", "not_ready") and llm.prompts == []
    assert (await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper.id))).error == "not_ready"


async def test_a_ready_paper_without_chunks_fails_with_no_text(session):
    paper = Paper(title="Bare", file_path="x.pdf", status=PaperStatus.READY, abstract="An abstract only.")
    session.add(paper)
    await session.flush()
    llm = Scripted()
    view = await evidence_cards.build_card(session, paper.id, llm=llm)
    assert (view.status, view.error) == ("failed", "no_text") and llm.prompts == []


async def test_two_invalid_replies_fail_the_card_as_model_error(session, paper):
    view, llm = await build(session, paper, Scripted("not json", "still not json"))
    assert (view.status, view.error) == ("failed", "model_error") and len(llm.prompts) == 2
    row = await session.scalar(select(LLMOutput).where(LLMOutput.id == view.output_id))
    assert json.loads(row.content) == {"error": "model_error"}


async def test_a_generation_stores_the_card_and_its_provenance_row(session, paper):
    view, _ = await build(session, paper, card_reply(**FULL))
    card = await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper.id))
    assert (card.status, card.provenance, card.model, card.prompt_version) == ("ready", "llm", "fake:scripted", 2)
    assert card.output_id == view.output_id and card.fields["sample"]["n"] == 120
    row = await session.get(LLMOutput, card.output_id)
    assert (row.kind, row.paper_id, row.prompt_version) == ("evidence_card", paper.id, 2)
    assert json.loads(row.content)["sample"]["n"] == 120 and row.source_chunks


async def test_regenerating_replaces_the_card_row(session, paper):
    await build(session, paper, card_reply(**FULL))
    await build(session, paper, card_reply(**{**FULL, "design": None}))
    cards = select(func.count()).select_from(EvidenceCard).where(EvidenceCard.paper_id == paper.id)
    count = await session.scalar(cards)
    assert count == 1


async def test_record_false_writes_nothing(session, paper):
    view, _ = await build(session, paper, card_reply(**FULL), record=False)
    assert view.status == "ready" and view.output_id is None and view.fields["sample"]["n"] == 120
    assert await session.scalar(select(EvidenceCard).where(EvidenceCard.paper_id == paper.id)) is None
    assert await session.scalar(select(LLMOutput).where(LLMOutput.paper_id == paper.id)) is None


async def test_an_invented_outcome_or_statistic_number_drops_the_effect_size(session, paper):
    for key, text in (("outcome", "reduced by 40%"), ("statistic", "odds ratio 7.5")):
        bad = {**FULL["effect_sizes"][0], key: text}
        view, _ = await build(session, paper, card_reply(**{**FULL, "effect_sizes": [bad]}))
        assert view.fields["effect_sizes"][0]["flags"] == ["number_not_in_source"], key


async def test_an_effect_size_with_no_number_in_its_value_is_dropped_as_no_statistic(session, paper):
    prose = {**FULL["effect_sizes"][0], "value": "Ratings were higher for humans"}
    view, _ = await build(session, paper, card_reply(**{**FULL, "effect_sizes": [prose, FULL["effect_sizes"][0]]}))
    assert [e["value"] for e in view.fields["effect_sizes"]] == [None, "d = 0.45"]
    assert view.fields["effect_sizes"][0]["flags"] == ["no_statistic"]
    assert view.fields["effect_sizes"][0]["quote"] is None


def test_prompt_v2_asks_for_the_statistic_as_printed():
    from app.core import prompts

    text = prompts.load("evidence_card", 2)
    assert "exactly as the paper prints it" in text and "participants" in text
