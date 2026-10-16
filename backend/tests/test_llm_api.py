import json
import logging
import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import parse_sse
from sqlalchemy import select

from app import main
from app.api import llm as llm_api
from app.config import settings
from app.core.errors import NotFound
from app.models import LLMModel
from app.providers import ollama_admin

pytestmark = pytest.mark.anyio

KEY = "sk-test-SECRET123"
OLLAMA_URL = "http://ollama.test:11434"
COMPATIBLE_URL = "https://api.example.com/v1"
owner_rows = pytest.mark.xdist_group("llm_connections")


def unique_label() -> str:
    return f"Test connection {uuid.uuid4().hex[:8]}"


async def create(client, **fields) -> dict:
    body = {"kind": "openai_compatible", "label": unique_label(), "base_url": COMPATIBLE_URL, "api_key": KEY, **fields}
    response = await client.post("/api/llm/connections", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def ollama(client) -> dict:
    return await create(client, kind="ollama", base_url=OLLAMA_URL, api_key=None)


@pytest.fixture
def pulls_in_test_transaction(session, monkeypatch):
    """A finished pull adds its model in a fresh session after the stream; keep it inside the test transaction."""

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(llm_api, "SessionLocal", test_session)


async def test_create_list_patch_and_delete_a_connection(client):
    created = await create(client, base_url=f"{COMPATIBLE_URL}/")

    assert created == {
        "id": created["id"], "kind": "openai_compatible", "label": created["label"], "base_url": COMPATIBLE_URL,
        "has_key": True, "key_hint": "T123", "is_local": False, "models": [],
    }
    listed = (await client.get("/api/llm/connections")).json()
    assert [c for c in listed if c["id"] == created["id"]] == [created]

    path = f"/api/llm/connections/{created['id']}"
    renamed = (await client.patch(path, json={"label": f"{created['label']} renamed"})).json()
    assert (renamed["label"], renamed["has_key"], renamed["key_hint"]) == (f"{created['label']} renamed", True, "T123")
    replaced = (await client.patch(path, json={"api_key": "sk-new-key-4321"})).json()
    assert replaced["key_hint"] == "4321"
    cleared = (await client.patch(path, json={"api_key": None, "base_url": "http://localhost:1234/v1"})).json()
    assert (cleared["has_key"], cleared["key_hint"], cleared["is_local"]) == (False, None, True)

    assert (await client.delete(path)).status_code == 204
    assert (await client.delete(path)).json() == {"detail": "connection_not_found"}


@pytest.mark.parametrize(
    ("create_fields", "patch", "expected"),
    [
        ({"kind": "ollama", "base_url": OLLAMA_URL, "api_key": None}, {"base_url": "http://192.168.1.9:11434"},
         {"base_url": "http://192.168.1.9:11434", "has_key": False, "is_local": True}),
        ({"kind": "anthropic", "base_url": None}, {"api_key": "sk-ant-new-9876"},
         {"base_url": None, "has_key": True, "key_hint": "9876", "is_local": False}),
        ({"kind": "openai_compatible"}, {"api_key": None}, {"has_key": False, "key_hint": None, "is_local": False}),
    ],
)
async def test_each_kind_is_created_patched_and_deleted(client, create_fields, patch, expected):
    connection = await create(client, **create_fields)
    path = f"/api/llm/connections/{connection['id']}"

    patched = await client.patch(path, json=patch)

    assert patched.status_code == 200
    assert {key: patched.json()[key] for key in expected} == expected
    assert patched.json()["kind"] == create_fields["kind"]
    assert (await client.delete(path)).status_code == 204
    assert (await client.patch(path, json=patch)).status_code == 404


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"kind": "gemini"}, None),
        ({"label": "   "}, None),
        ({"base_url": "not a url"}, "base_url must be an http:// or https:// address"),
        ({"kind": "anthropic"}, "an Anthropic connection uses Anthropic's own address, so base_url must be empty"),
        ({"kind": "anthropic", "base_url": None, "api_key": None}, "an Anthropic connection needs an API key"),
        ({"kind": "ollama"}, "an Ollama connection doesn't take an API key"),
        ({"api_key": ""}, "an API key can't be empty; send null to remove it"),
        ({"is_local": True}, None),
    ],
)
async def test_create_refuses_bad_kinds_addresses_and_keys(client, body, detail):
    payload = {"kind": "openai_compatible", "label": unique_label(), "base_url": COMPATIBLE_URL, "api_key": KEY, **body}

    response = await client.post("/api/llm/connections", json=payload)

    assert response.status_code == 422
    if detail is not None:
        assert response.json() == {"detail": detail}


async def test_patch_refusals(client):
    first, second = await create(client), await create(client)
    path = f"/api/llm/connections/{second['id']}"

    assert (await client.patch(path, json={"label": first["label"]})).status_code == 409
    assert (await client.patch(path, json={"label": first["label"]})).json() == {"detail": "connection_label_taken"}
    for body in [{"kind": "ollama"}, {"api_key": ""}, {"label": None}, {"base_url": None}]:
        assert (await client.patch(path, json=body)).status_code == 422, body
    missing = f"/api/llm/connections/{uuid.uuid4()}"
    assert (await client.patch(missing, json={"label": "x"})).json() == {"detail": "connection_not_found"}
    assert (await client.patch(missing, json={"label": "x"})).status_code == 404


async def test_create_with_a_taken_label_is_a_conflict(client):
    taken = await create(client)

    response = await client.post(
        "/api/llm/connections", json={"kind": "ollama", "label": taken["label"], "base_url": OLLAMA_URL}
    )

    assert (response.status_code, response.json()) == (409, {"detail": "connection_label_taken"})


async def test_models_are_added_listed_for_chat_and_removed(client):
    connection = await create(client, base_url="http://192.168.1.5:1234/v1", api_key=None)
    models_path = f"/api/llm/connections/{connection['id']}/models"

    added = await client.post(models_path, json={"name": "  qwen3-8b  "})
    assert added.status_code == 201
    model = added.json()
    assert model == {"id": model["id"], "name": "qwen3-8b", "is_default": False}
    assert (await client.post(models_path, json={"name": "qwen3-8b"})).json() == {"detail": "model_taken"}
    assert (await client.post(models_path, json={"name": "   "})).status_code == 422
    missing_connection = f"/api/llm/connections/{uuid.uuid4()}/models"
    assert (await client.post(missing_connection, json={"name": "m"})).status_code == 404

    chat_models = [m for m in (await client.get("/api/llm/models")).json() if m["id"] == model["id"]]
    assert chat_models == [
        {"id": model["id"], "name": "qwen3-8b", "connection_label": connection["label"], "is_local": True,
         "is_default": False}
    ]

    assert (await client.delete(f"/api/llm/models/{model['id']}")).status_code == 204
    missing = await client.delete(f"/api/llm/models/{model['id']}")
    assert (missing.status_code, missing.json()) == (404, {"detail": "model_not_found"})


@owner_rows
async def test_put_default_moves_it_and_404s_an_unknown_model(client, session):
    connection = await create(client)
    path = f"/api/llm/connections/{connection['id']}/models"
    first = (await client.post(path, json={"name": "a"})).json()
    second = (await client.post(path, json={"name": "b"})).json()

    assert (await client.put("/api/llm/default", json={"model_id": first["id"]})).json()["is_default"] is True
    moved = await client.put("/api/llm/default", json={"model_id": second["id"]})
    assert moved.json() == {**second, "is_default": True}

    defaults = [m for m in (await client.get("/api/llm/models")).json() if m["is_default"]]
    assert [m["id"] for m in defaults] == [second["id"]]
    missing = await client.put("/api/llm/default", json={"model_id": str(uuid.uuid4())})
    assert (missing.status_code, missing.json()) == (404, {"detail": "model_not_found"})


@owner_rows
async def test_deleting_the_default_models_connection_leaves_no_default(client, session):
    connection = await create(client)
    model = (await client.post(f"/api/llm/connections/{connection['id']}/models", json={"name": "a"})).json()
    await client.put("/api/llm/default", json={"model_id": model["id"]})

    await client.delete(f"/api/llm/connections/{connection['id']}")

    assert not any(m["is_default"] for m in (await client.get("/api/llm/models")).json())


@pytest.mark.parametrize("path", ["test", "available"])
async def test_an_omniroute_server_on_this_machine_counts_as_cloud_once_it_answers(client, provider, path):
    """Found live on 2026-10-16: OmniRoute at host.docker.internal forwards every request to cloud providers, but its
    address made it "local", so a pre-screen sent abstracts out with no warning. Its own response header gives it
    away; a plain local server (LM Studio, llama.cpp) stays local."""
    omniroute = await create(client, base_url="http://host.docker.internal:20128/v1")
    plain = await create(client, base_url="http://localhost:1234/v1")
    assert (omniroute["is_local"], plain["is_local"]) == (True, True)  # by address, before anything answered
    model_list = {"json": {"data": [{"id": "auto/best-free"}]}}

    provider.reply("/v1/models", 200, headers={"x-omniroute-route-class": "CLIENT_API"}, **model_list)
    await call_model_list(client, omniroute["id"], path)
    provider.reply("/v1/models", 200, **model_list)
    await call_model_list(client, plain["id"], path)

    rows = {c["id"]: c for c in (await client.get("/api/llm/connections")).json()}
    assert (rows[omniroute["id"]]["is_local"], rows[plain["id"]]["is_local"]) == (False, True)
    model = (await client.post(f"/api/llm/connections/{omniroute['id']}/models", json={"name": "auto/best-free"})).json()
    chat_models = {m["id"]: m for m in (await client.get("/api/llm/models")).json()}
    assert chat_models[model["id"]]["is_local"] is False


async def call_model_list(client, connection_id: str, path: str) -> None:
    method = client.post if path == "test" else client.get
    assert (await method(f"/api/llm/connections/{connection_id}/{path}")).status_code == 200


async def test_test_connection_reports_the_count_no_list_or_the_reason(client, provider):
    connection = await create(client)
    path = f"/api/llm/connections/{connection['id']}/test"

    provider.reply("/v1/models", 200, json={"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]})
    assert (await client.post(path)).json() == {"ok": True, "model_count": 2, "message": "Connected · 2 models"}
    provider.reply("/v1/models", 200, json={"data": [{"id": "gpt-5"}]})
    assert (await client.post(path)).json()["message"] == "Connected · 1 model"
    provider.reply("/v1/models", 404, text="Not Found")
    assert (await client.post(path)).json() == {
        "ok": True, "model_count": None, "message": "No model list here; type model names by hand"
    }
    provider.reply("/v1/models", 401, json={"error": {"message": "bad key"}})
    assert (await client.post(path)).json() == {
        "ok": False, "model_count": None, "message": f"Key rejected by {connection['label']}"
    }
    provider.refuse("/v1/models")
    assert (await client.post(path)).json() == {
        "ok": False, "model_count": None, "message": "Can't reach api.example.com"
    }
    assert provider.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert (await client.post(f"/api/llm/connections/{uuid.uuid4()}/test")).status_code == 404


async def test_available_lists_the_providers_models_or_null_or_a_502(client, provider):
    connection = await ollama(client)
    path = f"/api/llm/connections/{connection['id']}/available"

    provider.reply("/api/tags", 200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}]})
    assert (await client.get(path)).json() == {"models": ["llama3.1:8b", "qwen3:8b"]}
    provider.reply("/api/tags", 404, text="page not found")
    assert (await client.get(path)).json() == {"models": None}
    provider.refuse("/api/tags")
    refused = await client.get(path)
    assert (refused.status_code, refused.json()) == (502, {"detail": "Can't reach ollama.test"})
    assert (await client.get(f"/api/llm/connections/{uuid.uuid4()}/available")).status_code == 404


async def pull(client, connection_id: str, name: str = "qwen3:8b"):
    return await client.post(f"/api/llm/connections/{connection_id}/pull", json={"name": name})


async def test_pull_restreams_ollama_progress_as_sse_in_order_and_adds_the_model(
    client, provider, session, pulls_in_test_transaction
):
    connection = await ollama(client)
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling a1b2", "digest": "sha256:a1b2", "total": 200, "completed": 50},
        {"status": "pulling a1b2", "digest": "sha256:a1b2", "total": 200, "completed": 200},
        {"status": "success"},
    ]
    provider.reply("/api/pull", 200, text="".join(json.dumps(line) + "\n" for line in lines))

    response = await pull(client, connection["id"], "hf.co/org/model:Q4_K_M")

    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["progress"] * 4 + ["done"]
    assert [data for _, data in events[:4]] == [
        {"status": "pulling manifest", "total": None, "completed": None},
        {"status": "pulling a1b2", "total": 200, "completed": 50},
        {"status": "pulling a1b2", "total": 200, "completed": 200},
        {"status": "success", "total": None, "completed": None},
    ]
    model = events[-1][1]["model"]
    assert (model["name"], model["is_default"]) == ("hf.co/org/model:Q4_K_M", False)
    listed = (await client.get("/api/llm/connections")).json()
    assert [m["name"] for c in listed if c["id"] == connection["id"] for m in c["models"]] == ["hf.co/org/model:Q4_K_M"]
    assert json.loads(provider.requests[0].content) == {"model": "hf.co/org/model:Q4_K_M", "stream": True}


async def test_pull_error_line_mid_stream_is_an_error_event_and_adds_nothing(client, provider, session):
    connection = await ollama(client)
    body = json.dumps({"status": "pulling manifest"}) + "\n" + json.dumps({"error": "file does not exist"}) + "\n"
    provider.reply("/api/pull", 200, text=body)

    events = parse_sse((await pull(client, connection["id"], "nope")).text)

    assert events == [
        ("progress", {"status": "pulling manifest", "total": None, "completed": None}),
        ("error", {"message": "Ollama: file does not exist"}),
    ]
    assert list(await session.scalars(select(LLMModel).where(LLMModel.name == "nope"))) == []


async def test_pull_when_ollama_is_not_running_is_an_error_event(client, provider):
    connection = await ollama(client)
    provider.refuse("/api/pull")

    assert parse_sse((await pull(client, connection["id"])).text) == [
        ("error", {"message": "Can't reach ollama.test"})
    ]


async def test_the_transaction_ends_before_the_download_streams(
    client, provider, session, pulls_in_test_transaction, monkeypatch
):
    """ollama_connection commits in the dependency, so a download of minutes holds no pooled connection."""
    connection = await ollama(client)
    provider.reply("/api/pull", 200, text=json.dumps({"status": "success"}) + "\n")
    commits = []
    real_commit = session.commit

    async def spy_commit():
        commits.append(len(provider.requests))  # how many pull requests had gone out when this commit ran
        await real_commit()

    monkeypatch.setattr(session, "commit", spy_commit)

    events = parse_sse((await pull(client, connection["id"])).text)

    assert [name for name, _ in events] == ["progress", "done"]
    assert commits == [0, 1]  # the dependency's commit before the download, then add_model's


async def test_a_progress_line_without_a_status_is_an_error_event_and_adds_nothing(client, provider, session):
    connection = await ollama(client)
    provider.reply("/api/pull", 200, text=json.dumps({"total": 100, "completed": 20}) + "\n")

    events = parse_sse((await pull(client, connection["id"], "nope")).text)

    assert events == [("error", {"message": "The download stopped because of an unexpected error"})]
    assert list(await session.scalars(select(LLMModel).where(LLMModel.name == "nope"))) == []


async def test_a_failure_after_the_download_is_an_error_event(client, provider, session, monkeypatch):
    """The connection deleted while the download ran: the progress bar is told instead of hanging on no event."""
    connection = await ollama(client)
    provider.reply("/api/pull", 200, text=json.dumps({"status": "success"}) + "\n")

    async def deleted_meanwhile(*args, **kwargs):
        raise NotFound("connection_not_found")

    monkeypatch.setattr(llm_api.llm_connections, "add_model", deleted_meanwhile)

    events = parse_sse((await pull(client, connection["id"], "nope")).text)

    assert [name for name, _ in events] == ["progress", "error"]
    assert events[-1][1] == {"message": "The model was downloaded but couldn't be added to chat"}


async def test_pull_refusals_before_the_stream(client):
    compatible, connection = await create(client), await ollama(client)

    assert (await pull(client, str(uuid.uuid4()))).status_code == 404
    not_ollama = await pull(client, compatible["id"])
    assert (not_ollama.status_code, not_ollama.json()) == (422, {"detail": "not_an_ollama_connection"})
    assert (await pull(client, connection["id"], "  ")).status_code == 422


async def delete_installed(client, connection_id: str, name: str):
    return await client.delete(f"/api/llm/connections/{connection_id}/installed", params={"name": name})


async def test_delete_installed_removes_it_from_ollama_and_from_chat(client, provider, session):
    connection = await ollama(client)
    await client.post(f"/api/llm/connections/{connection['id']}/models", json={"name": "hf.co/org/model:Q4"})
    provider.reply("/api/delete", 200)

    response = await delete_installed(client, connection["id"], "hf.co/org/model:Q4")

    assert response.status_code == 204
    assert json.loads(provider.requests[0].content) == {"model": "hf.co/org/model:Q4"}
    listed = (await client.get("/api/llm/connections")).json()
    assert [c["models"] for c in listed if c["id"] == connection["id"]] == [[]]


async def test_delete_installed_refusals(client, provider):
    compatible, connection = await create(client), await ollama(client)

    assert (await delete_installed(client, str(uuid.uuid4()), "qwen3:8b")).json() == {"detail": "connection_not_found"}
    provider.reply("/api/delete", 404, json={"error": "model 'qwen3:8b' not found"})
    not_installed = await delete_installed(client, connection["id"], "qwen3:8b")
    assert (not_installed.status_code, not_installed.json()) == (404, {"detail": "model_not_installed"})
    assert (await delete_installed(client, compatible["id"], "qwen3:8b")).status_code == 422
    assert (await delete_installed(client, connection["id"], "")).status_code == 422
    provider.refuse("/api/delete")
    refused = await delete_installed(client, connection["id"], "qwen3:8b")
    assert (refused.status_code, refused.json()) == (502, {"detail": "Can't reach ollama.test"})


async def test_the_fake_stack_pulls_without_ollama(client, session, pulls_in_test_transaction, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(ollama_admin, "FAKE_PULL_DELAY", 0)
    connection = await ollama(client)

    events = parse_sse((await pull(client, connection["id"], "fake-pulled")).text)

    assert [name for name, _ in events] == ["progress"] * len(ollama_admin.FAKE_PULL) + ["done"]
    assert events[-1][1]["model"]["name"] == "fake-pulled"


async def test_a_pull_for_search_adds_nothing_to_chat(client, provider, pulls_in_test_transaction):
    """Settings → Search pulls nomic-embed-text with add_to_chat false: an embedding model never lands in chat."""
    connection = await ollama(client)
    provider.reply("/api/pull", 200, text=json.dumps({"status": "success"}) + "\n")

    response = await pull_for_search(client, connection["id"])

    assert parse_sse(response.text)[-1] == ("done", {"model": None})
    listed = (await client.get("/api/llm/connections")).json()
    assert [c["models"] for c in listed if c["id"] == connection["id"]] == [[]]


async def pull_for_search(client, connection_id: str):
    body = {"name": "nomic-embed-text", "add_to_chat": False}
    return await client.post(f"/api/llm/connections/{connection_id}/pull", json=body)


@owner_rows
async def test_a_stored_key_appears_in_no_llm_response_and_no_log_record(
    client, session, provider, caplog, pulls_in_test_transaction
):
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []

    async def call(method: str, path: str, **kwargs):
        response = await client.request(method, f"/api/llm{path}", **kwargs)
        bodies.append(response.text)
        return response

    created = await call("POST", "/connections", json={
        "kind": "openai_compatible", "label": unique_label(), "base_url": COMPATIBLE_URL, "api_key": KEY,
    })
    connection = created.json()
    path = f"/connections/{connection['id']}"
    ollama_connection = (await call("POST", "/connections", json={
        "kind": "ollama", "label": unique_label(), "base_url": OLLAMA_URL,
    })).json()

    # 422s that carry the key: a key where none is allowed, and keys in the wrong shape (FastAPI would echo them).
    refusals = [
        await call("POST", "/connections", json={"kind": "ollama", "label": unique_label(), "base_url": OLLAMA_URL,
                                                 "api_key": KEY}),
        await call("POST", "/connections", json={"kind": "openai_compatible", "label": unique_label(),
                                                 "base_url": COMPATIBLE_URL, "api_key": [KEY]}),
        await call("POST", "/connections", json={"kind": "openai_compatible", "label": unique_label(),
                                                 "base_url": COMPATIBLE_URL, "api_key": KEY, "extra": KEY}),
        await call("PATCH", path, json={"api_key": {"value": KEY}}),
        await call("PATCH", f"/connections/{ollama_connection['id']}", json={"api_key": KEY}),
    ]
    assert [r.status_code for r in refusals] == [422] * 5

    await call("GET", "/connections")
    await call("PATCH", path, json={"label": f"{connection['label']} renamed", "api_key": KEY})
    provider.reply("/v1/models", 401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})
    assert (await call("POST", f"{path}/test")).json()["ok"] is False
    provider.reply("/v1/models", 500, json={"error": {"message": f"upstream failed for key {KEY}"}})
    assert (await call("POST", f"{path}/test")).json()["message"].endswith("upstream failed for key ••••")
    assert (await call("GET", f"{path}/available")).status_code == 502
    model = (await call("POST", f"{path}/models", json={"name": "gpt-5"})).json()
    await call("GET", "/models")
    await call("PUT", "/default", json={"model_id": model["id"]})
    provider.reply("/api/pull", 200, text=json.dumps({"status": "success"}) + "\n")
    await call("POST", f"/connections/{ollama_connection['id']}/pull", json={"name": "qwen3:8b"})
    provider.reply("/api/delete", 200)
    await call("DELETE", f"/connections/{ollama_connection['id']}/installed", params={"name": "qwen3:8b"})
    await call("DELETE", f"/models/{model['id']}")
    await call("DELETE", path)

    assert len(bodies) == 19  # every call above ran
    assert [body for body in bodies if KEY in body] == []
    assert KEY not in caplog.text
    assert "Key rejected by" in caplog.text  # the failures were logged, just without the key


async def test_the_api_seeds_the_first_connection_from_env_at_startup(monkeypatch):
    seeded = []

    class Pool:
        async def aclose(self):
            pass

    async def create_pool(_):
        return Pool()

    async def seed(session, *values):
        seeded.append(values)
        return False

    async def no_paper_sources_seed(*_):
        return False

    monkeypatch.setattr(main, "create_pool", create_pool)
    monkeypatch.setattr(main.llm_connections, "seed_from_env", seed)
    monkeypatch.setattr(main.paper_sources, "seed_from_env", no_paper_sources_seed)

    async with main.lifespan(main.create_app()):
        pass

    assert seeded == [(settings.llm_provider, settings.llm_model, settings.ollama_url, settings.anthropic_api_key)]
