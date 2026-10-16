"""Model connections: where chat models come from, which of their models chat lists, and the one default.

A view never carries a key, only has_key and key_hint. Key rules live here, so no error message can quote a key.
ponytail: keys are plain text in the local database, the same exposure as .env; encrypt them at rest (Fernet with a
SECRET_KEY from .env) if the database is ever backed up or shared.
"""

import ipaddress
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, InvalidInput, NotFound
from app.models import EmbeddingSource, LLMConnection, LLMModel
from app.providers.llm import host_of

logger = logging.getLogger(__name__)

# A hint is the key's last 4 characters, and only for keys of 8 or more, so it never shows most of a key.
KEY_HINT_CHARS = 4
KEY_HINT_MIN_LENGTH = 8
LOCAL_HOSTS = {"localhost", "host.docker.internal"}
SEED_LABELS = {"ollama": "Ollama", "anthropic": "Anthropic"}

# D151: embedding_source's foreign key would refuse the delete anyway, as a 500; this says why and where to go.
SEARCH_USES = "Search uses this connection. Choose another search source in Settings → Search first."


@dataclass(frozen=True)
class ModelView:
    id: uuid.UUID
    name: str
    is_default: bool


@dataclass(frozen=True)
class ConnectionView:
    id: uuid.UUID
    kind: str
    label: str
    base_url: str | None
    has_key: bool
    key_hint: str | None
    is_local: bool
    models: list[ModelView]  # by name


@dataclass(frozen=True)
class ChatModelView:
    """One entry of the chat dropdown."""

    id: uuid.UUID
    name: str
    connection_label: str
    is_local: bool
    is_default: bool


def key_hint(api_key: str | None) -> str | None:
    return api_key[-KEY_HINT_CHARS:] if api_key and len(api_key) >= KEY_HINT_MIN_LENGTH else None


def is_local(kind: str, base_url: str | None, cloud_proxy: bool = False) -> bool:
    """True for localhost, host.docker.internal, or a loopback or private IP. Anthropic is never local, and neither is
    a server that answered as a cloud router (`cloud_proxy`, set when its model list is read).

    ponytail: by host string only, no DNS, so `mybox.local` or a Tailscale 100.x address counts as cloud.
    """
    if kind == "anthropic" or base_url is None or cloud_proxy:
        return False
    host = urlsplit(base_url).hostname or ""
    if host in LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _check_url(kind: str, base_url: str | None) -> str | None:
    """The address without a trailing slash. Required (http or https) except for anthropic, where it must be empty.

    A query string or userinfo is refused: a key belongs in api_key, where it is masked, not in an address every
    response echoes back.
    """
    if kind == "anthropic":
        if base_url is not None:
            raise InvalidInput("an Anthropic connection uses Anthropic's own address, so base_url must be empty")
        return None
    url = (base_url or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and (parts.port or 0) >= 0
            and not parts.query
            and not parts.username
        )
    except ValueError:  # a malformed IPv6 host or a port that isn't a number
        valid = False
    if not valid:
        raise InvalidInput("base_url must be an http:// or https:// address")
    return url


def _check_key(kind: str, api_key: str | None) -> str | None:
    """The key without surrounding whitespace. The messages never include it."""
    if api_key is not None:
        api_key = api_key.strip()
        if not api_key:
            raise InvalidInput("an API key can't be empty; send null to remove it")
    if kind == "anthropic" and api_key is None:
        raise InvalidInput("an Anthropic connection needs an API key")
    if kind == "ollama" and api_key is not None:
        raise InvalidInput("an Ollama connection doesn't take an API key")
    return api_key


async def _views(session: AsyncSession, *where) -> list[ConnectionView]:
    connections = list(await session.scalars(select(LLMConnection).where(*where).order_by(LLMConnection.label)))
    models = await session.scalars(
        select(LLMModel).where(LLMModel.connection_id.in_([c.id for c in connections])).order_by(LLMModel.name)
    )
    by_connection: dict[uuid.UUID, list[ModelView]] = defaultdict(list)
    for model in models:
        by_connection[model.connection_id].append(ModelView(model.id, model.name, model.is_default))
    return [
        ConnectionView(
            id=c.id,
            kind=c.kind,
            label=c.label,
            base_url=c.base_url,
            has_key=c.api_key is not None,
            key_hint=key_hint(c.api_key),
            is_local=is_local(c.kind, c.base_url, c.cloud_proxy),
            models=by_connection[c.id],
        )
        for c in connections
    ]


async def _row(session: AsyncSession, connection_id: uuid.UUID) -> LLMConnection:
    connection = await session.get(LLMConnection, connection_id)
    if connection is None:
        raise NotFound("connection_not_found")
    return connection


async def _ensure_label_free(session: AsyncSession, label: str, connection_id: uuid.UUID | None = None) -> None:
    # ponytail: check-then-write; two concurrent creates would hit UNIQUE (label) as a 500. Single user.
    taken = select(LLMConnection.id).where(LLMConnection.label == label, LLMConnection.id != connection_id)
    if await session.scalar(taken) is not None:
        raise Conflict("connection_label_taken")


async def list_connections(session: AsyncSession) -> list[ConnectionView]:
    return await _views(session)


async def get_connection(session: AsyncSession, connection_id: uuid.UUID) -> ConnectionView:
    views = await _views(session, LLMConnection.id == connection_id)
    if not views:
        raise NotFound("connection_not_found")
    return views[0]


async def get_connection_row(session: AsyncSession, connection_id: uuid.UUID) -> LLMConnection:
    """The row with its key, for building a provider call. Never return it from a route."""
    return await _row(session, connection_id)


async def record_cloud_proxy(session: AsyncSession, connection: LLMConnection, cloud_proxy: bool) -> None:
    """What the server's last model list showed: a cloud router or not. Writes only on a change."""
    if connection.cloud_proxy != cloud_proxy:
        connection.cloud_proxy = cloud_proxy
        await session.commit()


async def create_connection(
    session: AsyncSession, kind: str, label: str, base_url: str | None, api_key: str | None
) -> ConnectionView:
    """Never calls the provider: an Ollama that isn't running must still be saveable. Raises InvalidInput, Conflict."""
    base_url, api_key = _check_url(kind, base_url), _check_key(kind, api_key)
    await _ensure_label_free(session, label)
    connection = LLMConnection(kind=kind, label=label, base_url=base_url, api_key=api_key)
    session.add(connection)
    await session.commit()
    return await get_connection(session, connection.id)


async def update_connection(session: AsyncSession, connection_id: uuid.UUID, changes: dict) -> ConnectionView:
    """`changes` holds only what the request sent (label, base_url, api_key): a missing api_key keeps the key and
    None clears it. Moving to a different host without sending a new key drops the stored key (the new address
    hasn't proven it accepts it); a path- or port-only change on the same host keeps it. Any new address forgets
    `cloud_proxy` until the new server's model list is read. The kind never changes. Raises NotFound, InvalidInput,
    Conflict."""
    connection = await _row(session, connection_id)
    if "label" in changes:
        await _ensure_label_free(session, changes["label"], connection_id)
    base_url = _check_url(connection.kind, changes.get("base_url", connection.base_url))
    host_changed = "base_url" in changes and host_of(base_url) != host_of(connection.base_url)
    key_input = None if (host_changed and "api_key" not in changes) else changes.get("api_key", connection.api_key)
    api_key = _check_key(connection.kind, key_input)
    connection.cloud_proxy = connection.cloud_proxy and base_url == connection.base_url
    connection.label = changes.get("label", connection.label)
    connection.base_url, connection.api_key = base_url, api_key
    connection.updated_at = func.now()
    await session.commit()
    return await get_connection(session, connection_id)


async def delete_connection(session: AsyncSession, connection_id: uuid.UUID) -> None:
    """Its models go too (ON DELETE CASCADE); if one was the default, there is no default afterwards. Refused while
    search uses it (Conflict(SEARCH_USES))."""
    connection = await _row(session, connection_id)
    in_use = select(EmbeddingSource.id).where(EmbeddingSource.connection_id == connection_id)
    if await session.scalar(in_use) is not None:
        raise Conflict(SEARCH_USES)
    await session.delete(connection)
    await session.commit()


async def list_chat_models(session: AsyncSession) -> list[ChatModelView]:
    rows = await session.execute(
        select(LLMModel, LLMConnection)
        .join(LLMConnection, LLMModel.connection_id == LLMConnection.id)
        .order_by(LLMConnection.label, LLMModel.name)
    )
    return [
        ChatModelView(m.id, m.name, c.label, is_local(c.kind, c.base_url, c.cloud_proxy), m.is_default)
        for m, c in rows
    ]


async def add_model(session: AsyncSession, connection_id: uuid.UUID, name: str, exist_ok: bool = False) -> ModelView:
    """Lists a model in chat. Raises NotFound, or Conflict("model_taken") unless exist_ok (a finished pull)."""
    await _row(session, connection_id)
    existing = await session.scalar(
        select(LLMModel).where(LLMModel.connection_id == connection_id, LLMModel.name == name)
    )
    if existing is not None:
        if not exist_ok:
            raise Conflict("model_taken")
        return ModelView(existing.id, existing.name, existing.is_default)
    model = LLMModel(connection_id=connection_id, name=name)
    session.add(model)
    await session.commit()
    return ModelView(model.id, model.name, model.is_default)


async def remove_model(session: AsyncSession, model_id: uuid.UUID) -> None:
    """Saved answers keep their copied model and connection names. Raises NotFound("model_not_found")."""
    model = await session.get(LLMModel, model_id)
    if model is None:
        raise NotFound("model_not_found")
    await session.delete(model)
    await session.commit()


async def remove_model_named(session: AsyncSession, connection_id: uuid.UUID, name: str) -> None:
    """After a model is deleted from Ollama's disk: drop it from chat too, if it was listed."""
    await session.execute(delete(LLMModel).where(LLMModel.connection_id == connection_id, LLMModel.name == name))
    await session.commit()


async def set_default(session: AsyncSession, model_id: uuid.UUID) -> ModelView:
    """Moves the default in one transaction. The old default is unset first: the partial unique index
    llm_models_one_default refuses a second true row. Raises NotFound("model_not_found")."""
    model = await session.get(LLMModel, model_id)
    if model is None:
        raise NotFound("model_not_found")
    await session.execute(
        update(LLMModel).where(LLMModel.is_default, LLMModel.id != model_id).values(is_default=False)
    )
    model.is_default = True
    await session.commit()
    return ModelView(model.id, model.name, model.is_default)


async def resolve(session: AsyncSession, model_id: uuid.UUID | None) -> tuple[LLMConnection, LLMModel]:
    """The connection and model a question uses: `model_id`, or the default when it is None.

    Raises NotFound("model_not_found") for an unknown id, Conflict("no_model") when there is no default.
    """
    query = select(LLMConnection, LLMModel).join(LLMModel, LLMModel.connection_id == LLMConnection.id)
    query = query.where(LLMModel.is_default) if model_id is None else query.where(LLMModel.id == model_id)
    row = (await session.execute(query)).first()
    if row is None:
        raise Conflict("no_model") if model_id is None else NotFound("model_not_found")
    return row[0], row[1]


async def seed_from_env(
    session: AsyncSession, provider: str, model: str, ollama_url: str, anthropic_api_key: str | None
) -> bool:
    """Creates one connection and its default model from .env, only while there are no connections at all.

    `fake` seeds nothing (the E2E stack runs on the owner's database), nor does `anthropic` without a key. The label
    conflict clause makes a second attempt, even a concurrent one, a no-op. Returns whether it seeded.
    """
    if provider == "fake" or await session.scalar(select(func.count()).select_from(LLMConnection)):
        return False
    if provider == "anthropic" and not anthropic_api_key:
        logger.warning("LLM_PROVIDER=anthropic has no ANTHROPIC_API_KEY, so no model connection was created")
        return False
    base_url, api_key = (None, anthropic_api_key) if provider == "anthropic" else (ollama_url.rstrip("/"), None)
    connection_id = await session.scalar(
        insert(LLMConnection)
        .values(kind=provider, label=SEED_LABELS[provider], base_url=base_url, api_key=api_key)
        .on_conflict_do_nothing(index_elements=["label"])
        .returning(LLMConnection.id)
    )
    if connection_id is None:
        return False
    session.add(LLMModel(connection_id=connection_id, name=model, is_default=True))
    await session.commit()
    logger.info("created the %s model connection from .env, with %s as the default model", provider, model)
    return True
