import logging
import uuid

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core import llm_connections as connections
from app.core.errors import Conflict, InvalidInput, NotFound
from app.db import engine
from app.models import LLMConnection, LLMModel

pytestmark = pytest.mark.anyio

KEY = "sk-test-SECRET123"
# The dev database holds the owner's connections and their default. Tests that move the default or empty the table
# lock those rows, so they share one xdist worker instead of waiting on each other's transactions.
owner_rows = pytest.mark.xdist_group("llm_connections")


def unique_label() -> str:
    return f"Test connection {uuid.uuid4().hex[:8]}"


async def openai_connection(session, api_key: str | None = KEY, **fields) -> connections.ConnectionView:
    values = {"label": unique_label(), "base_url": "https://api.example.com/v1", **fields}
    return await connections.create_connection(session, "openai_compatible", api_key=api_key, **values)


async def connection_with_model(session, name="model-a") -> tuple[connections.ConnectionView, connections.ModelView]:
    connection = await openai_connection(session)
    return connection, await connections.add_model(session, connection.id, name)


async def test_each_kind_is_created_with_its_address_and_key_rules(session):
    ollama = await connections.create_connection(
        session, "ollama", unique_label(), "http://host.docker.internal:11434/", None
    )
    anthropic = await connections.create_connection(session, "anthropic", unique_label(), None, f"  {KEY}\n")
    compatible = await openai_connection(session, base_url=" https://openrouter.ai/api/v1/ ")

    assert (ollama.kind, ollama.base_url, ollama.has_key, ollama.key_hint, ollama.is_local, ollama.models) == (
        "ollama", "http://host.docker.internal:11434", False, None, True, []
    )
    assert (anthropic.base_url, anthropic.has_key, anthropic.key_hint, anthropic.is_local) == (
        None, True, "T123", False
    )
    assert (compatible.base_url, compatible.is_local) == ("https://openrouter.ai/api/v1", False)
    stored = await session.get(LLMConnection, anthropic.id)
    assert stored.api_key == KEY  # stripped of the pasted whitespace
    assert "api_key" not in connections.ConnectionView.__dataclass_fields__


@pytest.mark.parametrize(
    ("kind", "base_url", "api_key", "message"),
    [
        ("anthropic", "https://api.anthropic.com", KEY, "base_url must be empty"),
        ("anthropic", None, None, "needs an API key"),
        ("ollama", None, None, "http:// or https://"),
        ("ollama", "http://localhost:11434", KEY, "doesn't take an API key"),
        ("openai_compatible", "ftp://example.com", None, "http:// or https://"),
        ("openai_compatible", "https://", None, "http:// or https://"),
        ("openai_compatible", "http://[::1", None, "http:// or https://"),
        ("openai_compatible", "http://example.com:port", None, "http:// or https://"),
        ("openai_compatible", "https://api.example.com/v1?key=abc", None, "http:// or https://"),
        ("openai_compatible", f"https://user:{KEY}@api.example.com/v1", None, "http:// or https://"),
        ("openai_compatible", "https://api.example.com", "", "can't be empty"),
        ("openai_compatible", "https://api.example.com", "   ", "can't be empty"),
    ],
)
async def test_invalid_addresses_and_keys_are_refused_without_quoting_the_key(
    session, kind, base_url, api_key, message
):
    with pytest.raises(InvalidInput, match=message) as refused:
        await connections.create_connection(session, kind, unique_label(), base_url, api_key)
    assert KEY not in str(refused.value)


async def test_a_db_error_on_a_key_bearing_write_masks_the_key_in_the_exception_and_the_log(session, caplog):
    # A NUL byte in base_url passes every Python-side check (label uniqueness included) and fails only at the
    # INSERT, whose parameter tuple also carries the key.
    with caplog.at_level(logging.ERROR):
        with pytest.raises(DBAPIError) as excinfo:
            await connections.create_connection(
                session, "openai_compatible", unique_label(), "https://api.example.com/v1\x00", KEY
            )
        logging.getLogger(__name__).exception("write failed", exc_info=excinfo.value)

    assert KEY not in str(excinfo.value)
    assert KEY not in caplog.text
    # The test engine sets hide_parameters itself, so the app's own engine needs this guard to stay honest.
    assert engine.sync_engine.hide_parameters is True


@pytest.mark.parametrize(("api_key", "hint"), [(KEY, "T123"), ("12345678", "5678"), ("1234567", None), (None, None)])
def test_the_key_hint_is_the_last_four_characters_and_never_most_of_a_short_key(api_key, hint):
    assert connections.key_hint(api_key) == hint


@pytest.mark.parametrize(
    ("kind", "base_url", "local"),
    [
        ("ollama", "http://localhost:11434", True),
        ("ollama", "http://host.docker.internal:11434", True),
        ("openai_compatible", "http://192.168.1.5:1234/v1", True),
        ("openai_compatible", "http://10.0.0.2:8000/v1", True),
        ("openai_compatible", "http://127.0.0.1:8080/v1", True),
        ("openai_compatible", "https://api.openai.com/v1", False),
        ("openai_compatible", "http://100.101.102.103:1234/v1", False),
        ("anthropic", None, False),
    ],
)
def test_is_local_goes_by_host_only(kind, base_url, local):
    assert connections.is_local(kind, base_url) is local


async def test_a_new_address_forgets_that_the_old_server_was_a_cloud_router(session):
    connection = await openai_connection(session, base_url="http://host.docker.internal:20128/v1")
    await connections.record_cloud_proxy(session, await connections.get_connection_row(session, connection.id), True)

    renamed = await connections.update_connection(session, connection.id, {"label": "Renamed " + connection.label})
    moved = await connections.update_connection(session, connection.id, {"base_url": "http://host.docker.internal:1234/v1"})

    assert (renamed.is_local, moved.is_local) == (False, True)


async def test_a_taken_label_is_a_conflict_on_create_and_on_rename(session):
    first, second = await openai_connection(session), await openai_connection(session)

    with pytest.raises(Conflict, match="^connection_label_taken$"):
        await openai_connection(session, label=first.label)
    with pytest.raises(Conflict, match="^connection_label_taken$"):
        await connections.update_connection(session, second.id, {"label": first.label})
    renamed = await connections.update_connection(session, first.id, {"label": first.label})  # its own label is fine
    assert renamed.label == first.label


async def test_patch_keeps_a_missing_key_clears_a_null_one_and_refuses_an_empty_one(session):
    connection = await openai_connection(session)

    kept = await connections.update_connection(session, connection.id, {"label": "Renamed " + connection.label})
    assert (kept.label, kept.has_key, kept.key_hint, kept.kind) == (
        "Renamed " + connection.label, True, "T123", "openai_compatible"
    )
    # A host move without a new key drops it (test_patch_to_a_new_host_drops_the_key_unless_a_new_one_is_sent
    # covers the rule); is_local still recomputes from the new address.
    moved = await connections.update_connection(session, connection.id, {"base_url": "http://localhost:1234/v1/"})
    assert (moved.base_url, moved.is_local, moved.has_key) == ("http://localhost:1234/v1", True, False)
    restored = await connections.update_connection(session, connection.id, {"api_key": KEY})
    assert (restored.has_key, restored.key_hint) == (True, "T123")
    with pytest.raises(InvalidInput, match="can't be empty"):
        await connections.update_connection(session, connection.id, {"api_key": ""})
    cleared = await connections.update_connection(session, connection.id, {"api_key": None})
    assert (cleared.has_key, cleared.key_hint) == (False, None)
    replaced = await connections.update_connection(session, connection.id, {"api_key": "new-key-9876"})
    assert (replaced.has_key, replaced.key_hint) == (True, "9876")


async def test_patch_to_a_new_host_drops_the_key_unless_a_new_one_is_sent(session):
    connection = await openai_connection(session)

    moved = await connections.update_connection(session, connection.id, {"base_url": "https://openrouter.ai/api/v1"})
    assert (moved.has_key, moved.key_hint) == (False, None)
    stored = await session.get(LLMConnection, connection.id)
    assert stored.api_key is None

    with_new_key = await connections.update_connection(
        session, connection.id, {"base_url": "https://groq.com/openai/v1", "api_key": "new-key-9876"}
    )
    assert (with_new_key.has_key, with_new_key.key_hint) == (True, "9876")

    same_host = await connections.update_connection(session, connection.id, {"base_url": "https://groq.com/openai/v2"})
    assert (same_host.has_key, same_host.key_hint) == (True, "9876")


async def test_patch_keeps_the_kind_rules(session):
    anthropic = await connections.create_connection(session, "anthropic", unique_label(), None, KEY)
    ollama = await connections.create_connection(session, "ollama", unique_label(), "http://localhost:11434", None)

    with pytest.raises(InvalidInput, match="needs an API key"):
        await connections.update_connection(session, anthropic.id, {"api_key": None})
    with pytest.raises(InvalidInput, match="base_url must be empty"):
        await connections.update_connection(session, anthropic.id, {"base_url": "https://example.com"})
    with pytest.raises(InvalidInput, match="doesn't take an API key"):
        await connections.update_connection(session, ollama.id, {"api_key": KEY})
    with pytest.raises(InvalidInput, match="http:// or https://"):
        await connections.update_connection(session, ollama.id, {"base_url": None})
    with pytest.raises(NotFound, match="^connection_not_found$"):
        await connections.update_connection(session, uuid.uuid4(), {"label": "x"})


async def test_models_are_added_once_listed_by_name_and_removed(session):
    connection, second = await connection_with_model(session, "model-b")
    first = await connections.add_model(session, connection.id, "model-a")

    with pytest.raises(Conflict, match="^model_taken$"):
        await connections.add_model(session, connection.id, "model-a")
    assert await connections.add_model(session, connection.id, "model-a", exist_ok=True) == first
    with pytest.raises(NotFound, match="^connection_not_found$"):
        await connections.add_model(session, uuid.uuid4(), "model-a")
    assert [m.name for m in (await connections.get_connection(session, connection.id)).models] == ["model-a", "model-b"]

    chat_models = [m for m in await connections.list_chat_models(session) if m.connection_label == connection.label]
    assert chat_models == [
        connections.ChatModelView(first.id, "model-a", connection.label, False, False),
        connections.ChatModelView(second.id, "model-b", connection.label, False, False),
    ]

    await connections.remove_model(session, first.id)
    await connections.remove_model_named(session, connection.id, "model-b")
    await connections.remove_model_named(session, connection.id, "never-listed")
    assert (await connections.get_connection(session, connection.id)).models == []
    with pytest.raises(NotFound, match="^model_not_found$"):
        await connections.remove_model(session, first.id)


async def test_deleting_a_connection_deletes_its_models(session):
    connection, model = await connection_with_model(session)

    await connections.delete_connection(session, connection.id)

    assert await session.get(LLMModel, model.id) is None
    with pytest.raises(NotFound, match="^connection_not_found$"):
        await connections.delete_connection(session, connection.id)
    with pytest.raises(NotFound, match="^connection_not_found$"):
        await connections.get_connection(session, connection.id)


@owner_rows
async def test_set_default_moves_the_one_default(session):
    _, first = await connection_with_model(session, "model-a")
    _, second = await connection_with_model(session, "model-b")

    await connections.set_default(session, first.id)
    assert (await connections.set_default(session, second.id)).is_default is True

    defaults = list(await session.scalars(select(LLMModel.id).where(LLMModel.is_default)))
    assert defaults == [second.id]
    with pytest.raises(NotFound, match="^model_not_found$"):
        await connections.set_default(session, uuid.uuid4())


@owner_rows
async def test_the_database_refuses_a_second_default(session):
    connection, first = await connection_with_model(session)
    await connections.set_default(session, first.id)

    # Raised out of the savepoint, so only the savepoint rolls back and the test transaction carries on.
    with pytest.raises(IntegrityError, match="llm_models_one_default"):
        async with session.begin_nested():
            await session.execute(insert(LLMModel).values(connection_id=connection.id, name="model-z", is_default=True))

    assert await session.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.is_default)) == 1


@owner_rows
async def test_deleting_the_default_model_or_its_connection_leaves_no_default(session):
    connection, model = await connection_with_model(session)
    await connections.set_default(session, model.id)
    await connections.remove_model(session, model.id)
    assert await session.scalar(select(LLMModel.id).where(LLMModel.is_default)) is None

    other, other_model = await connection_with_model(session)
    await connections.set_default(session, other_model.id)
    await connections.delete_connection(session, other.id)
    assert await session.scalar(select(LLMModel.id).where(LLMModel.is_default)) is None
    assert connection.id != other.id


@owner_rows
async def test_resolve_picks_the_asked_model_or_the_default(session):
    connection, asked = await connection_with_model(session, "asked")
    _, default = await connection_with_model(session, "the-default")
    await connections.set_default(session, default.id)

    found_connection, found_model = await connections.resolve(session, asked.id)
    assert (found_connection.id, found_connection.label, found_model.name) == (connection.id, connection.label, "asked")
    assert (await connections.resolve(session, None))[1].id == default.id
    with pytest.raises(NotFound, match="^model_not_found$"):
        await connections.resolve(session, uuid.uuid4())

    await session.execute(update(LLMModel).values(is_default=False))
    with pytest.raises(Conflict, match="^no_model$"):
        await connections.resolve(session, None)


@owner_rows
@pytest.mark.parametrize(
    ("provider", "key", "expected"),
    [
        ("ollama", None, ("ollama", "Ollama", "http://host.docker.internal:11434", None)),
        ("anthropic", KEY, ("anthropic", "Anthropic", None, KEY)),
    ],
)
async def test_seed_from_env_fills_an_empty_table_once(session, provider, key, expected):
    await session.execute(delete(LLMConnection))
    env = (provider, "qwen3:8b", "http://host.docker.internal:11434/", key)

    assert await connections.seed_from_env(session, *env) is True
    assert await connections.seed_from_env(session, *env) is False

    [connection] = await session.scalars(select(LLMConnection))
    assert (connection.kind, connection.label, connection.base_url, connection.api_key) == expected
    [model] = await session.scalars(select(LLMModel))
    assert (model.connection_id, model.name, model.is_default) == (connection.id, "qwen3:8b", True)


@owner_rows
async def test_seed_from_env_leaves_existing_connections_alone(session):
    await session.execute(delete(LLMConnection))
    mine = await openai_connection(session)

    assert await connections.seed_from_env(session, "ollama", "qwen3:8b", "http://localhost:11434", None) is False
    assert list(await session.scalars(select(LLMConnection.id))) == [mine.id]


@owner_rows
async def test_seed_from_env_seeds_nothing_for_fake_or_for_anthropic_without_a_key(session, caplog):
    await session.execute(delete(LLMConnection))

    assert await connections.seed_from_env(session, "fake", "qwen3:8b", "http://localhost:11434", None) is False
    with caplog.at_level(logging.WARNING, logger="app.core.llm_connections"):
        assert await connections.seed_from_env(session, "anthropic", "claude-sonnet-5", "http://x", None) is False
    assert await session.scalar(select(func.count()).select_from(LLMConnection)) == 0
    assert "ANTHROPIC_API_KEY" in caplog.text
