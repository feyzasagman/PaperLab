"""Model connections, the models chat lists, the default, and Ollama's pull and delete. No response has a key."""

import logging
import uuid
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import SessionDep, TransportDep
from app.core import llm_connections
from app.core.errors import InvalidInput
from app.db import SessionLocal
from app.models import LLMConnection
from app.providers import llm, ollama_admin
from app.providers.base import LLMError, LLMUnavailable
from app.schemas.llm import (
    AvailableModelsOut,
    ChatModelOut,
    ConnectionCheckOut,
    ConnectionCreate,
    ConnectionOut,
    ConnectionUpdate,
    DefaultModelIn,
    ModelCreate,
    ModelOut,
    PullDoneEvent,
    PullErrorEvent,
    PullProgressEvent,
    PullRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])

NO_MODEL_LIST = "No model list here; type model names by hand"


def _log_failure(connection: LLMConnection, exc: Exception) -> None:
    # Provider messages are masked; the label and host say which connection, never its key or headers.
    logger.warning("model connection %s (%s): %s", connection.label, llm.host_of(connection.base_url), exc)


async def ollama_connection(connection_id: uuid.UUID, session: SessionDep) -> LLMConnection:
    """404 for an unknown connection, 422 for one that isn't Ollama: checked before any stream starts."""
    connection = await llm_connections.get_connection_row(session, connection_id)
    if connection.kind != "ollama":
        raise InvalidInput("not_an_ollama_connection")
    # End the transaction now. A download can take minutes and must not hold a pooled connection; the pull route
    # reads the row's values into locals before the first event.
    await session.commit()
    return connection


OllamaDep = Annotated[LLMConnection, Depends(ollama_connection)]


@router.get("/models")
async def list_chat_models(session: SessionDep) -> list[ChatModelOut]:
    return await llm_connections.list_chat_models(session)


@router.get("/connections")
async def list_connections(session: SessionDep) -> list[ConnectionOut]:
    return await llm_connections.list_connections(session)


@router.post("/connections", status_code=201)
async def create_connection(payload: ConnectionCreate, session: SessionDep) -> ConnectionOut:
    key = None if payload.api_key is None else payload.api_key.get_secret_value()
    return await llm_connections.create_connection(session, payload.kind, payload.label, payload.base_url, key)


@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: uuid.UUID, payload: ConnectionUpdate, session: SessionDep) -> ConnectionOut:
    changes = {field: getattr(payload, field) for field in payload.model_fields_set}
    if changes.get("api_key") is not None:
        changes["api_key"] = changes["api_key"].get_secret_value()
    return await llm_connections.update_connection(session, connection_id, changes)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: uuid.UUID, session: SessionDep) -> Response:
    await llm_connections.delete_connection(session, connection_id)
    return Response(status_code=204)


@router.post("/connections/{connection_id}/test")
async def test_connection(connection_id: uuid.UUID, session: SessionDep, transport: TransportDep) -> ConnectionCheckOut:
    """Lists the provider's models. A failure is a 200 with ok false and the reason, since the check itself worked."""
    connection = await llm_connections.get_connection_row(session, connection_id)
    try:
        listed = await llm.list_models(connection, transport=transport)
    except (LLMUnavailable, LLMError) as exc:
        _log_failure(connection, exc)
        return ConnectionCheckOut(ok=False, model_count=None, message=str(exc))
    await llm_connections.record_cloud_proxy(session, connection, listed.cloud_proxy)
    models = listed.names
    if models is None:
        return ConnectionCheckOut(ok=True, model_count=None, message=NO_MODEL_LIST)
    count = f"{len(models)} model" if len(models) == 1 else f"{len(models)} models"
    return ConnectionCheckOut(ok=True, model_count=len(models), message=f"Connected · {count}")


@router.get("/connections/{connection_id}/available")
async def available_models(
    connection_id: uuid.UUID, session: SessionDep, transport: TransportDep
) -> AvailableModelsOut:
    """The provider's model list (Ollama: its installed models). 502 with the reason when the provider fails."""
    connection = await llm_connections.get_connection_row(session, connection_id)
    try:
        listed = await llm.list_models(connection, transport=transport)
    except (LLMUnavailable, LLMError) as exc:
        _log_failure(connection, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None
    await llm_connections.record_cloud_proxy(session, connection, listed.cloud_proxy)
    return AvailableModelsOut(models=listed.names)


@router.post("/connections/{connection_id}/models", status_code=201)
async def add_model(connection_id: uuid.UUID, payload: ModelCreate, session: SessionDep) -> ModelOut:
    return await llm_connections.add_model(session, connection_id, payload.name)


@router.delete("/models/{model_id}", status_code=204)
async def remove_model(model_id: uuid.UUID, session: SessionDep) -> Response:
    await llm_connections.remove_model(session, model_id)
    return Response(status_code=204)


@router.put("/default")
async def set_default(payload: DefaultModelIn, session: SessionDep) -> ModelOut:
    return await llm_connections.set_default(session, payload.model_id)


# The route yields ServerSentEvent, so FastAPI can't see the payload models; listing them puts them in OpenAPI.
PULL_RESPONSES = {200: {"model": PullProgressEvent | PullDoneEvent | PullErrorEvent}}


def pull_error(message: str) -> ServerSentEvent:
    return ServerSentEvent(event="error", data=PullErrorEvent(message=message))


@router.post("/connections/{connection_id}/pull", response_class=EventSourceResponse, responses=PULL_RESPONSES)
async def pull_model(
    payload: PullRequest, connection: OllamaDep, transport: TransportDep
) -> AsyncIterable[ServerSentEvent]:
    """Events: progress (repeated), then done with the model, now listed in chat (null for Settings → Search's pull,
    which sends add_to_chat false). error replaces done, also when Ollama can't be reached, and then nothing is
    added."""
    connection_id, base_url, label = connection.id, connection.base_url, connection.label
    try:
        async for line in ollama_admin.pull(base_url, payload.name, transport=transport):
            progress = PullProgressEvent(**{key: line.get(key) for key in ("status", "total", "completed")})
            yield ServerSentEvent(event="progress", data=progress)
    except (LLMUnavailable, LLMError) as exc:
        logger.warning("pulling %s on %s (%s): %s", payload.name, label, llm.host_of(base_url), exc)
        yield pull_error(str(exc))
        return
    except Exception:
        logger.exception("pulling %s on %s (%s) failed unexpectedly", payload.name, label, llm.host_of(base_url))
        yield pull_error("The download stopped because of an unexpected error")
        return

    if not payload.add_to_chat:  # an embedding model never lands in chat's dropdown (D152)
        yield ServerSentEvent(event="done", data=PullDoneEvent(model=None))
        return

    try:
        async with SessionLocal() as session:
            model = await llm_connections.add_model(session, connection_id, payload.name, exist_ok=True)
    except Exception:
        # The connection can be deleted while the download runs; without this the client waits on an event
        # that never comes.
        logger.exception("adding %s to %s after its download failed", payload.name, label)
        yield pull_error("The model was downloaded but couldn't be added to chat")
        return
    yield ServerSentEvent(event="done", data=PullDoneEvent(model=model))


@router.delete("/connections/{connection_id}/installed", status_code=204)
async def delete_installed(
    connection: OllamaDep,
    session: SessionDep,
    transport: TransportDep,
    name: Annotated[str, Query(min_length=1)],
) -> Response:
    """Deletes the model from Ollama's disk and from chat. `name` is a query parameter: Ollama names can hold `/`."""
    try:
        deleted = await ollama_admin.delete(connection.base_url, name, transport=transport)
    except (LLMUnavailable, LLMError) as exc:
        _log_failure(connection, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="model_not_installed")
    await llm_connections.remove_model_named(session, connection.id, name)
    return Response(status_code=204)
