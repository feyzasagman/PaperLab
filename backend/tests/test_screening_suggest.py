import pytest

from app.core.screening_suggest import SYSTEM_PROMPT, Suggestion, build_prompt, build_prompt_v2, parse_reply, suggest
from app.providers.llm import FakeLLM

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(("text", "expected"), [
    ("INCLUDE\nAn RCT in adults.", Suggestion("include", None, "An RCT in adults.")),
    ("exclude wrong_study_type\nA review.", Suggestion("exclude", "wrong_study_type", "A review.")),
    ("\n  EXCLUDE   Wrong_Topic  \n\n Off topic. \n", Suggestion("exclude", "wrong_topic", "Off topic.")),
    ("UNSURE\nNo abstract.", Suggestion("unsure", None, "No abstract.")),
    ("INCLUDE", Suggestion("include", None, None)),
    ("EXCLUDE\nno reason given", Suggestion("unsure", None, None)),
    ("EXCLUDE bad_reason\nx", Suggestion("unsure", None, None)),
    ("Sure! I think this should be excluded.", Suggestion("unsure", None, None)),
    ("", Suggestion("unsure", None, None)),
])  # fmt: skip
def test_parse_reply(text, expected):
    assert parse_reply(text) == expected


def test_note_is_cut_to_300_characters():
    assert len(parse_reply("INCLUDE\n" + "x" * 500).note) == 300


def test_prompt_carries_criteria_title_and_abstract_and_tolerates_braces():
    prompt = build_prompt("RCTs {adults}", "Sleep {trial}", None)
    assert "RCTs {adults}" in prompt and "Sleep {trial}" in prompt and "(no abstract)" in prompt


def test_system_prompt_lists_every_reason_and_the_reply_format():
    for reason in ("wrong_topic", "wrong_study_type", "duplicate", "language", "inaccessible", "other"):
        assert reason in SYSTEM_PROMPT
    assert "INCLUDE" in SYSTEM_PROMPT and "UNSURE" in SYSTEM_PROMPT


async def test_fake_llm_answers_the_screening_prompt_in_format():
    text = "".join([t async for t in FakeLLM().stream(SYSTEM_PROMPT, build_prompt("c", "t", "a"))])
    assert parse_reply(text) == Suggestion("exclude", "wrong_study_type", "A review, not a primary study.")


class Scripted:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    async def stream(self, system, prompt):
        self.prompts.append(prompt)
        yield self.replies.pop(0)


ABSTRACT = "We study how large language models homogenize creative writing. Outputs were 23% less diverse."


async def test_a_valid_suggestion_keeps_its_verified_quotes():
    reply = ('{"relevance": 5, "decision": "include", "reason": null, '
             '"quotes": ["large language models homogenize creative writing", "Outputs were 23% less diverse"], '
             '"why": "On topic."}')
    s = await suggest(Scripted(reply), "Do LLMs homogenize creativity?", "LLM creativity", "", "T", ABSTRACT)
    assert (s.verdict, s.relevance, s.confidence, s.flags) == ("include", 5, "high", [])
    assert s.quotes == ["large language models homogenize creative writing", "Outputs were 23% less diverse"]


async def test_a_quote_matches_through_case_and_curly_quotes_but_a_paraphrase_is_dropped():
    reply = ('{"relevance": 4, "decision": "include", "reason": null, '
             '"quotes": ["LARGE language models homogenize creative writing", "LLMs make writing samey"], "why": "x"}')
    s = await suggest(Scripted(reply), "q", "i", "e", "T", ABSTRACT)
    assert s.quotes == ["LARGE language models homogenize creative writing"]
    assert s.confidence == "low" and s.flags == ["quote_dropped"]


async def test_an_exclude_with_no_valid_quote_is_low_confidence():
    reply = '{"relevance": 1, "decision": "exclude", "reason": "wrong_topic", "quotes": [], "why": "x"}'
    s = await suggest(Scripted(reply), "q", "i", "e", "T", ABSTRACT)
    assert (s.verdict, s.reason, s.confidence) == ("exclude", "wrong_topic", "low")


async def test_no_abstract_is_unsure_without_calling_the_model():
    llm = Scripted()
    s = await suggest(llm, "q", "i", "e", "T", None)
    assert (s.verdict, s.flags, s.relevance) == ("unsure", ["no_abstract"], None) and llm.prompts == []


async def test_a_model_that_never_answers_json_is_unsure_with_model_error():
    s = await suggest(Scripted("nope", "still nope"), "q", "i", "e", "T", ABSTRACT)
    assert (s.verdict, s.flags) == ("unsure", ["model_error"])


async def test_a_reason_on_an_include_is_dropped_and_an_unknown_reason_is_refused():
    ok = await suggest(Scripted('{"relevance": 4, "decision": "include", "reason": "other", "quotes": [], "why": "x"}'),
                       "q", "i", "e", "T", ABSTRACT)
    assert ok.reason is None
    boring = '{"relevance": 2, "decision": "exclude", "reason": "boring", "quotes": [], "why": "x"}'
    bad = await suggest(Scripted(boring, boring), "q", "i", "e", "T", ABSTRACT)
    assert bad.flags == ["model_error"]


def test_build_prompt_v2_tolerates_braces_in_the_abstract_and_criteria():
    prompt = build_prompt_v2("q {x}", "RCTs {adults}", None, "T {t}", "Set {1, 2} was used")
    assert "Set {1, 2} was used" in prompt and "RCTs {adults}" in prompt and "(none given)" in prompt
