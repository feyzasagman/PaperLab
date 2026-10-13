"""JSON answers from a model, validated, with one retry (M39 F3, D228)."""

import pytest
from pydantic import BaseModel, Field

from app.core.llm_json import ask_json

pytestmark = pytest.mark.anyio


class Answer(BaseModel):
    score: int = Field(ge=1, le=5)


class Why(BaseModel):
    why: str
    score: int = Field(ge=1, le=5)


class Scripted:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    async def stream(self, system, prompt):
        self.prompts.append(prompt)
        yield self.replies.pop(0)


@pytest.mark.parametrize("reply", ['{"score": 4}', 'Sure! ```json\n{"score": 4}\n``` hope that helps', ' {"score":4} '])
async def test_the_first_json_object_is_used(reply):
    assert (await ask_json(Scripted(reply), "sys", "p", Answer)) == Answer(score=4)


async def test_braces_inside_a_string_with_escaped_quotes_are_skipped():
    reply = r'{"why": "he said \"hi\" {ok}", "score": 4}'
    assert (await ask_json(Scripted(reply), "sys", "p", Why)) == Why(why='he said "hi" {ok}', score=4)


async def test_one_retry_carries_the_error():
    llm = Scripted('{"score": 9}', '{"score": 3}')
    assert (await ask_json(llm, "sys", "p", Answer)) == Answer(score=3)
    assert len(llm.prompts) == 2 and "score" in llm.prompts[1] and llm.prompts[1].startswith("p")


async def test_two_bad_replies_give_none():
    assert await ask_json(Scripted("no json here", '{"score": "x"}'), "sys", "p", Answer) is None


async def test_a_brace_right_after_an_escaped_quote_stays_inside_the_string():
    reply = r'{"why": "a \"} b", "score": 4}'
    assert (await ask_json(Scripted(reply), "sys", "p", Why)) == Why(why='a "} b', score=4)
