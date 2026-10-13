import json

import httpx
import httpx2
import pytest

from app.config import settings
from app.core import notes
from app.core.chat import SYSTEM_PROMPT, WORKSPACE_SYSTEM_PROMPT
from app.providers import llm
from app.providers.base import LLMError, LLMUnavailable

pytestmark = pytest.mark.anyio

OLLAMA_URL = "http://ollama.test:11434"


async def collect(model, tokens: list[str]) -> list[str]:
    async for token in model.stream("You cite sources.", "Question: why?"):
        tokens.append(token)
    return tokens


def ndjson(*lines: dict) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def token_line(text: str) -> dict:
    return {"model": "qwen3:8b", "message": {"role": "assistant", "content": text}, "done": False}


def ollama(handler) -> llm.OllamaLLM:
    return llm.OllamaLLM("qwen3:8b", OLLAMA_URL, transport=httpx.MockTransport(handler))


async def test_ollama_streams_content_until_the_done_line():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = ndjson(
            token_line(""),  # thinking models can send empty content first
            token_line("Attention [C"),
            token_line("1]."),
            {"model": "qwen3:8b", "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        )
        return httpx.Response(200, text=body, headers={"content-type": "application/x-ndjson"})

    assert await collect(ollama(handler), []) == ["Attention [C", "1]."]
    assert str(requests[0].url) == f"{OLLAMA_URL}/api/chat"
    assert json.loads(requests[0].content) == {
        "model": "qwen3:8b",
        "messages": [
            {"role": "system", "content": "You cite sources."},
            {"role": "user", "content": "Question: why?"},
        ],
        "stream": True,
        "think": False,
        "options": {"num_ctx": llm.OLLAMA_NUM_CTX},
    }


async def test_ollama_missing_model_is_unavailable():
    model = ollama(lambda request: httpx.Response(404, json={"error": "model 'qwen3:8b' not found"}))

    with pytest.raises(LLMUnavailable, match=r"^Model qwen3:8b isn't available on Ollama$"):
        await collect(model, [])


async def test_ollama_connection_refused_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailable, match="^Can't reach ollama.test$"):
        await collect(ollama(handler), [])


def test_ollama_uses_a_generous_read_timeout_for_cold_model_loads():
    assert llm.OLLAMA_TIMEOUT == httpx.Timeout(120, connect=5)


async def test_ollama_error_line_mid_stream_is_an_llm_error():
    body = ndjson(token_line("Attention"), {"error": "an error was encountered while running the model"})
    model = ollama(lambda request: httpx.Response(200, text=body))
    tokens = []

    with pytest.raises(LLMError, match="an error was encountered while running the model"):
        await collect(model, tokens)
    assert tokens == ["Attention"]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def anthropic_body(texts: list[str], error: dict | None = None) -> str:
    message = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    body = sse("message_start", {"type": "message_start", "message": message})
    body += sse("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}})
    for text in texts:
        body += sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": text}})
    if error:
        return body + sse("error", {"type": "error", "error": error})
    body += sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    body += sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                  "usage": {"output_tokens": 5}})
    return body + sse("message_stop", {"type": "message_stop"})


def anthropic_model(handler) -> llm.AnthropicLLM:
    # The anthropic SDK 1.x runs on httpx2, so its fake transport comes from httpx2 too.
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return llm.AnthropicLLM("claude-sonnet-5", "sk-test", http_client=client)


async def test_anthropic_streams_text():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, text=anthropic_body(["Attention [C", "1]."]),
                               headers={"content-type": "text/event-stream"})

    assert await collect(anthropic_model(handler), []) == ["Attention [C", "1]."]
    assert requests[0]["system"] == "You cite sources."
    assert requests[0]["messages"] == [{"role": "user", "content": "Question: why?"}]
    assert (requests[0]["model"], requests[0]["stream"]) == ("claude-sonnet-5", True)


async def test_anthropic_overload_mid_stream_is_an_llm_error():
    overloaded = {"type": "overloaded_error", "message": "Overloaded"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        # Arrives inside a 200 stream, so the SDK raises the generic APIStatusError, not OverloadedError.
        return httpx2.Response(200, text=anthropic_body(["Attention [C"], error=overloaded),
                               headers={"content-type": "text/event-stream"})

    tokens = []
    with pytest.raises(LLMError, match="Overloaded"):
        await collect(anthropic_model(handler), tokens)
    assert tokens == ["Attention [C"]


class _DropAfterOneChunk(httpx2.AsyncByteStream):
    """A transport that delivers some bytes, then dies — not an SSE `error` event."""

    def __init__(self, chunk: bytes, message: str = "connection dropped"):
        self._chunk = chunk
        self._message = message

    async def __aiter__(self):
        yield self._chunk
        raise httpx2.ReadError(self._message)


async def test_anthropic_transport_error_mid_stream_is_an_llm_error():
    message = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    chunk = (
        sse("message_start", {"type": "message_start", "message": message})
        + sse("content_block_start", {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": ""}})
        + sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": "Attention"}})
    ).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=_DropAfterOneChunk(chunk))

    tokens = []
    with pytest.raises(LLMError, match="connection dropped"):
        await collect(anthropic_model(handler), tokens)
    assert tokens == ["Attention"]


async def test_anthropic_transport_error_mid_stream_masks_the_key():
    msg = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    chunk = (
        sse("message_start", {"type": "message_start", "message": msg})
        + sse("content_block_start", {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": ""}})
    ).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=_DropAfterOneChunk(chunk, message=f"connection dropped, key was {KEY}"))

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    adapter = llm.AnthropicLLM("claude-sonnet-5", KEY, "Claude", http_client=client)

    with pytest.raises(LLMError, match="^Claude: connection dropped, key was ••••$") as raised:
        await collect(adapter, [])
    assert KEY not in str(raised.value)


async def test_anthropic_unknown_model_is_unavailable():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "model"}})

    with pytest.raises(LLMUnavailable, match="^Model claude-sonnet-5 isn't available on Anthropic$"):
        await collect(anthropic_model(handler), [])


async def test_fake_llm_splits_the_citation_marker_and_records_calls():
    fake = llm.FakeLLM()

    tokens = await collect(fake, [])

    assert "".join(tokens) == llm.FAKE_ANSWER
    assert ["[C", "1]"] == tokens[tokens.index("[C") : tokens.index("[C") + 2]
    assert fake.calls == [("You cite sources.", "Question: why?")]


async def test_fake_llm_gives_the_workspace_answer_to_the_workspace_prompt():
    fake = llm.FakeLLM()

    tokens = [token async for token in fake.stream(WORKSPACE_SYSTEM_PROMPT, "Question: why?")]

    assert "".join(tokens) == llm.FAKE_WORKSPACE_ANSWER
    assert llm.FAKE_WORKSPACE_ANSWER == (
        "Fake workspace answer: both papers describe the method [C1][C2], as your note says [N1]."
    )
    assert tokens[tokens.index("[C") : tokens.index("[C") + 4] == ["[C", "1]", "[C", "2]"]
    assert ["[N", "1]"] == tokens[tokens.index("[N") : tokens.index("[N") + 2]


async def test_fake_llm_can_fail_mid_stream():
    tokens = []

    with pytest.raises(LLMError, match="fake model failed mid-answer"):
        await collect(llm.FakeLLM(fail_after=2), tokens)
    assert len(tokens) == 2


async def test_anthropic_max_tokens_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_max_tokens", 1234)
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, text=anthropic_body(["ok"]), headers={"content-type": "text/event-stream"})

    await collect(anthropic_model(handler), [])
    assert requests[0]["max_tokens"] == 1234


async def test_anthropic_rejected_key_names_the_connection_without_the_key():
    def handler(request: httpx2.Request) -> httpx2.Response:
        error = {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}
        return httpx2.Response(401, json={"type": "error", "error": error})

    with pytest.raises(LLMUnavailable, match="^Key rejected by Anthropic$"):
        await collect(anthropic_model(handler), [])


KEY = "sk-test-SECRET123"
COMPATIBLE_URL = "https://api.example.com/v1"


def data_lines(*chunks: dict | str) -> str:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)


def delta(content: str | None = None, **fields) -> dict:
    return {"choices": [{"index": 0, "delta": {**({"content": content} if content is not None else {}), **fields}}]}


def compatible(handler, api_key: str | None = KEY) -> llm.OpenAICompatibleLLM:
    return llm.OpenAICompatibleLLM("gpt-5-mini", COMPATIBLE_URL, api_key, "OpenRouter", httpx.MockTransport(handler))


async def test_openai_compatible_streams_content_deltas_in_order_and_stops_at_done():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (
            data_lines(delta("", role="assistant"), delta(reasoning_content="thinking"), delta("Attention [C"))
            + ": keep-alive\n\n"
            + data_lines(delta("1]."), "[DONE]", delta("after the end"))
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    assert await collect(compatible(handler), []) == ["Attention [C", "1]."]
    assert str(requests[0].url) == f"{COMPATIBLE_URL}/chat/completions"
    assert requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(requests[0].content) == {
        "model": "gpt-5-mini",
        "messages": [
            {"role": "system", "content": "You cite sources."},
            {"role": "user", "content": "Question: why?"},
        ],
        "stream": True,
    }


async def test_openai_compatible_sends_no_authorization_header_without_a_key():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=data_lines(delta("ok"), "[DONE]"))

    assert await collect(compatible(handler, api_key=None), []) == ["ok"]
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize(
    ("status", "body", "error", "message"),
    [
        (401, {"error": {"message": f"Incorrect API key {KEY}"}}, LLMUnavailable, "^Key rejected by OpenRouter$"),
        (403, {"error": {"message": "forbidden"}}, LLMUnavailable, "^Key rejected by OpenRouter$"),
        (404, {"error": {"message": "no model"}}, LLMUnavailable, "^Model gpt-5-mini isn't available on OpenRouter$"),
        (429, {"error": {"message": f"slow down {KEY}"}}, LLMError, "^OpenRouter returned 429: slow down ••••$"),
        (500, "upstream exploded", LLMError, "^OpenRouter returned 500: upstream exploded$"),
    ],
)
async def test_openai_compatible_error_statuses_in_words_never_quoting_the_key(status, body, error, message):
    reply = httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    with pytest.raises(error, match=message) as raised:
        await collect(compatible(lambda request: reply), [])
    assert KEY not in str(raised.value)


async def test_openai_compatible_error_chunk_mid_stream_is_an_llm_error():
    body = data_lines(delta("Attention"), {"error": {"message": f"Provider returned error for {KEY}", "code": 502}})
    tokens = []

    with pytest.raises(LLMError, match="^OpenRouter: Provider returned error for ••••$"):
        await collect(compatible(lambda request: httpx.Response(200, text=body)), tokens)
    assert tokens == ["Attention"]


async def test_openai_compatible_connection_refused_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailable, match="^Can't reach api.example.com$"):
        await collect(compatible(handler), [])


async def test_openai_compatible_line_that_isnt_json_is_an_llm_error():
    body = data_lines(delta("Attention")) + "data: {not json\n\n"
    tokens = []

    with pytest.raises(LLMError, match="^OpenRouter request failed: "):
        await collect(compatible(lambda request: httpx.Response(200, text=body)), tokens)
    assert tokens == ["Attention"]


async def test_openai_compatible_httpx_error_masks_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"boom {KEY}", request=request)

    with pytest.raises(LLMError, match="^OpenRouter request failed: boom ••••$") as raised:
        await collect(compatible(handler), [])
    assert KEY not in str(raised.value)


class Row:
    """Stands in for an LLMConnection row: build_llm and list_models only read these four fields."""

    def __init__(self, kind: str, label: str, base_url: str | None = None, api_key: str | None = None):
        self.kind, self.label, self.base_url, self.api_key = kind, label, base_url, api_key


@pytest.mark.parametrize(
    ("row", "expected", "host"),
    [
        (Row("ollama", "Ollama", "http://host.docker.internal:11434"), llm.OllamaLLM, "host.docker.internal"),
        (Row("openai_compatible", "LM Studio", "http://192.168.1.5:1234/v1"), llm.OpenAICompatibleLLM, "192.168.1.5"),
        (Row("anthropic", "Claude", None, KEY), llm.AnthropicLLM, "api.anthropic.com"),
    ],
)
def test_build_llm_makes_the_connections_adapter_with_its_label(row, expected, host):
    adapter = llm.build_llm(row, "some-model")

    assert isinstance(adapter, expected)
    assert (adapter.model, adapter.connection_name, adapter.host) == ("some-model", row.label, host)
    assert llm.build_llm(row, "some-model") is not adapter  # a new adapter per question


def test_build_llm_in_fake_mode_reports_the_fake_and_the_connection(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "fake")

    adapter = llm.build_llm(Row("anthropic", "Claude", None, KEY), "claude-sonnet-5")

    assert isinstance(adapter, llm.FakeLLM)
    assert (adapter.model, adapter.connection_name) == ("fake:claude-sonnet-5", "Claude")


def listing(handler, row: Row):
    return llm.list_models(row, transport=httpx.MockTransport(handler))


async def test_list_models_reads_ollama_tags_and_openai_compatible_models():
    ollama_requests, compatible_requests = [], []

    def tags(request: httpx.Request) -> httpx.Response:
        ollama_requests.append(request)
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "hf.co/a/b:Q4"}]})

    def models(request: httpx.Request) -> httpx.Response:
        compatible_requests.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]})

    assert await listing(tags, Row("ollama", "Ollama", OLLAMA_URL)) == ["hf.co/a/b:Q4", "qwen3:8b"]
    assert await listing(models, Row("openai_compatible", "OpenAI", COMPATIBLE_URL, KEY)) == ["gpt-5", "gpt-5-mini"]
    assert str(ollama_requests[0].url) == f"{OLLAMA_URL}/api/tags"
    assert "authorization" not in ollama_requests[0].headers
    assert str(compatible_requests[0].url) == f"{COMPATIBLE_URL}/models"
    assert compatible_requests[0].headers["authorization"] == f"Bearer {KEY}"


async def test_list_models_is_none_when_the_server_has_no_list():
    row = Row("openai_compatible", "llama.cpp", "http://localhost:8080/v1")
    assert await listing(lambda request: httpx.Response(404, text="Not Found"), row) is None


@pytest.mark.parametrize(
    ("reply", "error", "message"),
    [
        (httpx.Response(401, json={"error": {"message": f"bad {KEY}"}}), LLMUnavailable, "^Key rejected by OpenAI$"),
        (httpx.Response(500, json={"error": {"message": f"boom {KEY}"}}), LLMError, "^OpenAI returned 500: boom ••••$"),
        (httpx.Response(200, json={"unexpected": True}), LLMError, "^OpenAI sent a model list PaperLab can't read$"),
    ],
)
async def test_list_models_failures_in_words_never_quoting_the_key(reply, error, message):
    with pytest.raises(error, match=message) as raised:
        await listing(lambda request: reply, Row("openai_compatible", "OpenAI", COMPATIBLE_URL, KEY))
    assert KEY not in str(raised.value)


async def test_list_models_cant_reach_names_the_host():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailable, match="^Can't reach ollama.test$"):
        await listing(handler, Row("ollama", "Ollama", OLLAMA_URL))


async def test_list_models_httpx_error_masks_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"boom {KEY}", request=request)

    with pytest.raises(LLMError, match="^OpenAI request failed: boom ••••$") as raised:
        await listing(handler, Row("openai_compatible", "OpenAI", COMPATIBLE_URL, KEY))
    assert KEY not in str(raised.value)


def anthropic_listing(handler):
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return llm.list_models(Row("anthropic", "Claude", None, KEY), http_client=client)


async def test_list_models_for_anthropic():
    page = {
        "data": [{"id": "claude-sonnet-5", "type": "model", "display_name": "S", "created_at": "2026-01-01T00:00:00Z"}],
        "has_more": False, "first_id": "claude-sonnet-5", "last_id": "claude-sonnet-5",
    }
    assert await anthropic_listing(lambda request: httpx2.Response(200, json=page)) == ["claude-sonnet-5"]
    not_found = {"type": "error", "error": {"type": "not_found_error", "message": "no"}}
    assert await anthropic_listing(lambda request: httpx2.Response(404, json=not_found)) is None

    rejected = {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}}
    with pytest.raises(LLMUnavailable, match="^Key rejected by Claude$"):
        await anthropic_listing(lambda request: httpx2.Response(401, json=rejected))
    broken = {"type": "error", "error": {"type": "api_error", "message": f"boom {KEY}"}}
    with pytest.raises(LLMError, match="^Claude returned 500: boom ••••$"):
        await anthropic_listing(lambda request: httpx2.Response(500, json=broken))


async def test_anthropic_closes_its_client_after_a_stream_and_after_a_model_list():
    page = {"data": [], "has_more": False, "first_id": None, "last_id": None}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json=page)
        return httpx2.Response(200, text=anthropic_body(["ok"]), headers={"content-type": "text/event-stream"})

    streaming = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    listing = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    await collect(llm.AnthropicLLM("claude-sonnet-5", KEY, "Claude", http_client=streaming), [])
    await llm.list_models(Row("anthropic", "Claude", None, KEY), http_client=listing)

    assert (streaming.is_closed, listing.is_closed) == (True, True)


async def test_list_models_in_fake_mode_is_a_fixed_list_and_rejects_bad_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "fake")

    assert await llm.list_models(Row("ollama", "Ollama", OLLAMA_URL)) == llm.FAKE_MODELS
    with pytest.raises(LLMUnavailable, match="^Key rejected by Groq$"):
        await llm.list_models(Row("openai_compatible", "Groq", COMPATIBLE_URL, "bad-key"))


async def test_fake_llm_answers_a_question_asking_for_notes_with_two_suggested_notes_in_either_chat():
    fake = llm.FakeLLM()
    # The prompt always says "Notes (newest first)": only the question decides.
    asked = "Notes (newest first):\n\n(none)\n\nQuestion: Create notes to help me understand this paper\n"
    plain = "Notes (newest first):\n\n(none)\n\nQuestion: What anchors a note?\n"

    for system in (SYSTEM_PROMPT, WORKSPACE_SYSTEM_PROMPT):
        assert "".join([token async for token in fake.stream(system, asked)]) == llm.FAKE_NOTES_ANSWER
    assert "".join([token async for token in fake.stream(SYSTEM_PROMPT, plain)]) == llm.FAKE_ANSWER
    assert notes.note_blocks(llm.FAKE_NOTES_ANSWER) == [
        "The method anchors every note on a passage [C1].",
        "Keep one idea per note.",
    ]


async def test_fake_llm_answers_the_v2_screening_prompt_with_json_whose_quote_is_in_the_abstract():
    from app.core import screening_suggest as s

    abstract = "A controlled study of pagination in fixture search results."
    fake = llm.FakeLLM()
    results = {
        title: await s.suggest(fake, "q", "in", "out", title, abstract)
        for title in ("Fixture 1", "Fixture 2", "Fixture 3", "Fixture 4")
    }
    assert {r.verdict for r in results.values()} == {"include", "exclude", "unsure"}
    assert all(r.relevance and 1 <= r.relevance <= 5 for r in results.values())
    assert all(r.quotes == ["A controlled study"] for r in results.values() if r.verdict != "unsure")
    assert all(r.confidence == "high" for r in results.values() if r.verdict != "unsure")
    assert all(r.reason == "wrong_topic" for r in results.values() if r.verdict == "exclude")
