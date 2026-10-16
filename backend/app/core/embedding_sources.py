"""Which source search embeds with (M25, D151): one row, `embedding_source`, joined to the model connection that holds
its key. No row means Built-in. Every process (the API, the worker, the MCP server) reads it per question and per job,
so a switch applies everywhere with no restart and no signal.

A Source carries its connection's key for provider calls; a route returns a SourceView, which has none (M9's rule).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import InvalidInput
from app.core.llm_connections import get_connection_row, is_local
from app.models import EmbeddingSource, LLMConnection
from app.providers.llm import host_of

KINDS = ("builtin", "ollama", "openai", "gemini", "openai_compatible")
KIND_NAMES = {
    "builtin": "Built-in", "ollama": "Ollama", "openai": "OpenAI", "gemini": "Gemini",
    "openai_compatible": "OpenAI-compatible",
}  # fmt: skip
OLLAMA_MODEL = "nomic-embed-text"
# 768 dimensions only (D131): text-embedding-3 shortens to it; ada-002 can't, so it is never offered.
OPENAI_MODELS = ("text-embedding-3-small", "text-embedding-3-large")
GEMINI_MODEL = "gemini-embedding-2"  # gemini-embedding-001 shuts down 2028-05-14 (D154)
OPENAI_HOST = "api.openai.com"
GEMINI_HOST = "generativelanguage.googleapis.com"  # M9's Gemini preset: the same AI Studio key serves the native API
# D158: the probe a switch sends before anything changes. Never library text.
PROBE = "PaperLab checks that this search source works."

BUILT_IN_ALONE = "Built-in search takes no connection or model."
CONNECTION_REQUIRED = "Choose a connection for this search source."
NOT_ELIGIBLE = "{label} can't be used for {kind} search."
MODEL_NOT_ALLOWED = "{model} isn't a search model PaperLab can use with {kind}."
MODEL_REQUIRED = "Type the name of the model to search with."

# What each kind's name and label are when they don't come from the connection.
_LABELS = {"builtin": "Built-in", "openai": "OpenAI", "gemini": "Gemini"}
_FIXED_MODELS = {"ollama": OLLAMA_MODEL, "gemini": GEMINI_MODEL}


@dataclass(frozen=True)
class Source:
    kind: str
    connection_id: uuid.UUID | None
    connection_label: str | None
    base_url: str | None
    api_key: str | None  # for provider calls only: never return a Source from a route
    model: str | None
    name: str  # what chunks.embed_model records
    label: str  # in messages and copy: Built-in, OpenAI, Gemini, or the connection's label
    host: str | None
    is_local: bool
    rebuild_model: str | None = None  # D156
    rebuild_started_at: datetime | None = None
    error: str | None = None  # D155


@dataclass(frozen=True)
class SourceView:
    kind: str
    connection_id: uuid.UUID | None
    connection_label: str | None
    model: str | None
    host: str | None
    is_local: bool
    label: str


def name_for(kind: str, model: str | None, base_url: str | None) -> str:
    """What vectors from this source record (D151), so check_model, Re-index and the status work on one string. `@768`
    marks a model shortened to 768. A compatible server's name carries its address: another server behind the same
    model name may not give the same vectors. On the fake stack a connection's source records `fake:…`."""
    if kind == "builtin":
        return settings.embed_model
    name = {
        "ollama": f"ollama/{OLLAMA_MODEL}",
        "openai": f"openai/{model}@768",
        "gemini": f"gemini/{GEMINI_MODEL}@768",
    }.get(kind) or f"compat/{urlsplit(base_url).netloc}/{model}"
    return f"fake:{name}" if settings.llm_provider == "fake" else name


def _source(
    kind: str, connection: LLMConnection | None, model: str | None, row: EmbeddingSource | None = None
) -> Source:
    """A Source from a kind, its connection's current values and its model; `row` adds the rebuild and the error."""
    base_url = None if connection is None else connection.base_url
    return Source(
        kind=kind,
        connection_id=None if connection is None else connection.id,
        connection_label=None if connection is None else connection.label,
        base_url=base_url,
        api_key=None if connection is None else connection.api_key,
        model=model,
        name=name_for(kind, model, base_url),
        label=_LABELS.get(kind) or connection.label,
        host=None if connection is None else host_of(base_url),
        is_local=connection is None or is_local(connection.kind, base_url, connection.cloud_proxy),
        rebuild_model=None if row is None else row.rebuild_model,
        rebuild_started_at=None if row is None else row.rebuild_started_at,
        error=None if row is None else row.error,
    )


async def active(session: AsyncSession) -> Source:
    """The source search uses now: one select joined to its connection. No row: Built-in."""
    query = select(EmbeddingSource, LLMConnection).outerjoin(
        LLMConnection, LLMConnection.id == EmbeddingSource.connection_id
    )
    # populate_existing: after an upsert the identity map may still hold the row as it was loaded.
    found = (await session.execute(query.execution_options(populate_existing=True))).first()
    if found is None:
        return _source("builtin", None, None)
    row, connection = found
    return _source(row.kind, connection, row.model, row)


async def active_name(session: AsyncSession) -> str:
    return (await active(session)).name


def _suits(kind: str, connection: LLMConnection) -> bool:
    """D151: Ollama by its kind; OpenAI and Gemini by the compatible connection's host; any compatible server."""
    if kind == "ollama":
        return connection.kind == "ollama"
    if connection.kind != "openai_compatible":
        return False
    host = host_of(connection.base_url)
    return {"openai": host == OPENAI_HOST, "gemini": host == GEMINI_HOST}.get(kind, True)


def _model(kind: str, model: str | None) -> str:
    """The model this kind runs: fixed for Ollama and Gemini, one of OPENAI_MODELS (small by default), or the name a
    compatible server serves."""
    model = (model or "").strip() or None
    if kind in _FIXED_MODELS or kind == "openai":
        allowed = (_FIXED_MODELS[kind],) if kind in _FIXED_MODELS else OPENAI_MODELS
        if model is not None and model not in allowed:
            raise InvalidInput(MODEL_NOT_ALLOWED.format(model=model, kind=KIND_NAMES[kind]))
        return model or allowed[0]
    if model is None:
        raise InvalidInput(MODEL_REQUIRED)
    return model


async def candidate(session: AsyncSession, kind: str, connection_id: uuid.UUID | None, model: str | None) -> Source:
    """The source a switch asks for, checked before anything is sent (D158 step 1). Raises InvalidInput with a
    sentence, or NotFound("connection_not_found")."""
    if kind not in KINDS:
        raise InvalidInput(f"unknown search source {kind!r}")
    if kind == "builtin":
        if connection_id is not None or model is not None:
            raise InvalidInput(BUILT_IN_ALONE)
        return _source("builtin", None, None)
    if connection_id is None:
        raise InvalidInput(CONNECTION_REQUIRED)
    connection = await get_connection_row(session, connection_id)
    if not _suits(kind, connection):
        raise InvalidInput(NOT_ELIGIBLE.format(label=connection.label, kind=KIND_NAMES[kind]))
    return _source(kind, connection, _model(kind, model))


async def _upsert(session: AsyncSession, values: dict) -> None:
    insert_values = {"kind": "builtin", **values}  # no row yet means Built-in
    statement = insert(EmbeddingSource).values(insert_values).on_conflict_do_update(index_elements=["id"], set_=values)
    await session.execute(statement)
    await session.commit()


async def save(session: AsyncSession, source: Source) -> None:
    """Makes `source` the search source and starts a rebuild toward its name (D156), clearing the last error. A
    re-index saves the active source again, which only restarts the rebuild."""
    await _upsert(
        session,
        {
            "kind": source.kind,
            "connection_id": source.connection_id,
            "model": source.model,
            "rebuild_model": source.name,
            "rebuild_started_at": func.now(),
            "error": None,
            "error_at": None,
            "updated_at": func.now(),
        },
    )


async def record_error(session: AsyncSession, message: str) -> None:
    """The last embedding failure, as Settings shows it (D155). Provider messages are masked before they get here."""
    await _upsert(session, {"error": message, "error_at": func.now(), "updated_at": func.now()})


def view(source: Source) -> SourceView:
    """What a route may return: the source without its key."""
    return SourceView(
        kind=source.kind,
        connection_id=source.connection_id,
        connection_label=source.connection_label,
        model=source.model,
        host=source.host,
        is_local=source.is_local,
        label=source.label,
    )
