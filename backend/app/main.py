import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import charts, chat, datasets, discovery, embedding, graph, health, links, llm, notes, papers, workspaces
from app.api import mcp as mcp_api
from app.api import paper_sources as paper_sources_api
from app.api import reading_bridge as reading_bridge_api
from app.api import references as references_api
from app.api import saved_searches as saved_searches_api
from app.api import setup as setup_api
from app.api import workspace_search as workspace_search_api
from app.config import settings
from app.core import llm_connections, paper_sources
from app.core.errors import Conflict, DomainError, InvalidInput, NotFound
from app.db import SessionLocal

STATUS_BY_ERROR = {NotFound: 404, InvalidInput: 422, Conflict: 409}

# The only Host names the release image answers (spec §3): a web page in the user's browser can rebind its own name to
# 127.0.0.1, but it still sends that name, so it can't read the library as same-origin.
LOCAL_HOSTS = ["127.0.0.1", "localhost"]

# Methods a cross-site form or fetch can send with no preflight, so Origin is the only defence against them.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class FrontendFiles(StaticFiles):
    """The built frontend. index.html is revalidated on every load, so after an update the app's window never
    keeps an old page that points at hashed asset files the new image no longer has; the hashed files keep the
    default headers."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if os.path.basename(full_path) == "index.html":
            response.headers["Cache-Control"] = "no-cache"
        return response


def serve_frontend(app: FastAPI, folder: str) -> None:
    """The release image (FRONTEND_DIST): the frontend at / after every router, so /api/* always wins, only local Host
    names, and no cross-site write (a browser always sends Origin on those; curl and the MCP script, which send none,
    are unaffected). Both defences are added whenever FRONTEND_DIST is set, even if the folder itself is missing, so
    only the mount below is behind the isdir check. No folder at all: nothing changes."""
    if not folder:
        return
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin is not None and urlparse(origin).hostname not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        return await call_next(request)

    if not os.path.isdir(folder):
        return
    app.mount("/", FrontendFiles(directory=folder, html=True), name="frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    async with SessionLocal() as session:
        await llm_connections.seed_from_env(
            session, settings.llm_provider, settings.llm_model, settings.ollama_url, settings.anthropic_api_key
        )
        await paper_sources.seed_from_env(session, settings.openalex_mailto, settings.semantic_scholar_api_key)
    yield
    await app.state.arq.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="PaperLab", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(papers.router)
    app.include_router(reading_bridge_api.router)
    app.include_router(reading_bridge_api.workspace_router)
    app.include_router(notes.router)
    app.include_router(chat.router)
    app.include_router(workspaces.router)
    app.include_router(workspace_search_api.router)
    app.include_router(saved_searches_api.router)
    app.include_router(datasets.router)
    app.include_router(charts.router)
    app.include_router(llm.router)
    app.include_router(embedding.router)
    app.include_router(discovery.router)
    app.include_router(graph.router)
    app.include_router(paper_sources_api.router)
    app.include_router(references_api.router)
    app.include_router(links.router)
    app.include_router(mcp_api.router)
    app.include_router(setup_api.router)

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=STATUS_BY_ERROR.get(type(exc), 400))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own handler echoes each error's `input`, and a model-level error echoes the whole body: an API
        # key sent in the wrong shape would come back in the response. Same body otherwise.
        errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    serve_frontend(app, settings.frontend_dist)
    return app


app = create_app()
