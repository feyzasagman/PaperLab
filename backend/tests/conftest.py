import json
import math
import os
import random
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pymupdf
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, or_, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import chat as chat_api
from app.api.deps import get_transport, resolve_llm, resolve_llm_by_model_id
from app.config import settings
from app.core import discovery
from app.db import get_session
from app.main import create_app
from app.models import Author, Paper, PaperSources
from app.providers import arxiv, core_ac, crossref, embedding, openalex, semantic_scholar, unpaywall
from app.providers.base import LLMUnavailable
from app.providers.llm import FakeLLM
from app.workers import ingest

# The compose Postgres, published on the host. Every test runs inside a transaction that is
# rolled back afterwards, so tests can share the dev database without leaving rows behind.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://paperlab:paperlab@localhost:5433/paperlab"
)

# D159: every test starts on Built-in whatever the owner chose. An empty table of the same shape in the test
# connection's temporary schema hides the owner's row: Postgres searches pg_temp before public. Not a DELETE, which
# would hold the row's lock until the rollback while every other test's DELETE waited on it.
SHADOW_SEARCH_SOURCE = text("CREATE TEMP TABLE embedding_source (LIKE public.embedding_source INCLUDING ALL)")

OPENALEX_FIXTURES = Path(__file__).parent / "fixtures" / "openalex"
_RECORDINGS = "".join(path.read_text() for path in OPENALEX_FIXTURES.glob("*.json"))
RECORDED_IDS = sorted(set(re.findall(r"openalex\.org/([WA]\d+)", _RECORDINGS)))
RECORDED_DOIS = sorted({doi.lower() for doi in re.findall(r"doi\.org/(10\.[^\"]+)", _RECORDINGS)})

DISCOVERY_FIXTURES = Path(__file__).parent / "fixtures" / "discovery"

BODY_TEXT = "The quick brown fox jumps over the lazy dog near the river bank today. " * 3


@pytest.hookimpl(tryfirst=True)  # before xdist reads the markers
def pytest_collection_modifyitems(items):
    # Tests on the OpenAlex recordings all write the same UNIQUE papers and authors; on parallel workers their
    # transactions deadlock. `--dist loadgroup` (pyproject addopts) runs one xdist_group on a single worker.
    for item in items:
        # A test already in the "references" group stays only there: a second group name would make a third worker
        # ("openalex_references") that deadlocks with the "references" one on external_refs.
        if "fake_openalex" in item.fixturenames and not any(
            m.args == ("references",) for m in item.iter_markers("xdist_group")
        ):
            item.add_marker(pytest.mark.xdist_group("openalex"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_embedding_model(monkeypatch):
    """A test that forgets to pass a FakeEmbedder fails at once instead of loading the search model from
    MODELS_DIR."""

    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not load the embedding model; pass a FakeEmbedder or patch get_model")

    monkeypatch.setattr(embedding, "load", refuse)


@pytest.fixture
def database_url():
    return TEST_DATABASE_URL


@pytest.fixture
async def session():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, hide_parameters=True)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        # On the connection, outside any savepoint: a service's rollback must never drop it.
        await connection.execute(SHADOW_SEARCH_SOURCE)
        # create_savepoint: a service's session.commit() only releases a savepoint.
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as test_session:
            yield test_session
        await transaction.rollback()
    await engine.dispose()


class FakeArq:
    """Records enqueued jobs instead of talking to Redis (ASGITransport skips the lifespan)."""

    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name: str, *args) -> None:
        self.jobs.append((name, *args))


@pytest.fixture
def arq():
    return FakeArq()


def unit_vector(seed: str) -> list[float]:
    """A fixed pseudo-random 768-d unit vector per seed."""
    rng = random.Random(seed)
    vector = [rng.gauss(0, 1) for _ in range(768)]
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


class FakeEmbedder:
    """Stands in for any search source (a TextEmbedder, D150): records every encode call instead of running a model.

    A text embeds to `vectors[text]` when a test set one, otherwise to `unit_vector(text)`. Local, with the nomic
    prefixes, named "test" like the chunks tests write; a test makes a cloud one with is_local=False, or one named
    after the active source.
    """

    document_prefix, query_prefix = "search_document: ", "search_query: "

    def __init__(self, name: str = "test", label: str = "Test", is_local: bool = True, refuse: str | None = None):
        self.name = self.model = name
        self.label = label
        self.is_local = is_local
        self.refuse = refuse  # a message: encode fails with it, as a source that refuses
        self.calls: list[tuple[list[str], dict]] = []  # (texts, {}): M23's prefix assertions keep their form
        self.vectors: dict[str, list[float]] = {}

    async def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append((list(texts), {}))
        if self.refuse is not None:
            raise LLMUnavailable(self.refuse)
        return [self.vectors.get(t) or unit_vector(t) for t in texts]


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
async def worker_session(session, monkeypatch):
    """Point the worker at the rolled-back test session instead of its own SessionLocal. The owner's paper sources
    row (D15 shares the dev database) is hidden, so OpenAlex is off unless a test turns it on."""
    await session.execute(delete(PaperSources))

    @asynccontextmanager
    async def shared_session():
        yield session

    monkeypatch.setattr(ingest, "SessionLocal", shared_session)
    return session


@pytest.fixture
def ctx(embedder):
    """What WorkerSettings.on_startup leaves in the ARQ context, with the fake model."""
    return {"embedder": embedder}


def recorded(name: str) -> dict:
    """A response body recorded from OpenAlex by tests/fixtures/openalex/record.py."""
    return json.loads((OPENALEX_FIXTURES / f"{name}.json").read_text())


def recorded_discovery(name: str):
    """A body recorded by tests/fixtures/discovery/record.py."""
    return json.loads((DISCOVERY_FIXTURES / f"{name}.json").read_text())


class FakeOpenAlex:
    """OpenAlex behind httpx.MockTransport: serves recorded JSON by route and records every request. No network.

    A route key is a request path plus "?<filter>" for a filtered request, or the bare path to match any filter.
    An unrouted request fails loudly (AssertionError) instead of getting a default reply, so a path bug in the
    provider can't hide behind a plausible-looking 404. A route's replies are served in order and the last one
    repeats; a reply that is an exception is raised, the way httpx raises network errors and timeouts.
    """

    MAILTO = "paperlab-tests@example.com"

    def __init__(self):
        self.routes: dict[str, list] = {}
        self.requests: list[httpx.Request] = []
        # The worker opens its own client per ingest (D74): tests hand it this transport in ctx["transport"].
        self.transport = httpx.MockTransport(self._handle)
        self.client = openalex.new_client(email=self.MAILTO, transport=self.transport)

    def route(self, key: str, *replies) -> None:
        self.routes[key] = [
            r if isinstance(r, httpx.Response | Exception) else httpx.Response(200, json=r) for r in replies
        ]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        key = f"{path}?{request.url.params['filter']}" if "filter" in request.url.params else path
        replies = self.routes.get(key) or self.routes.get(path)
        if replies is None:
            raise AssertionError(f"unrouted OpenAlex request: {request.url}")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
async def fake_openalex(session):
    # D15 shares the dev database, which may hold the very papers and authors these recordings describe (enrich BERT
    # once and they're there). Hide them inside the test's rolled-back transaction so UNIQUE keys and the 30-day
    # author cache start clean.
    await session.execute(delete(Paper).where(or_(Paper.openalex_id.in_(RECORDED_IDS), Paper.doi.in_(RECORDED_DOIS))))
    await session.execute(delete(Author).where(Author.openalex_id.in_(RECORDED_IDS)))
    fake = FakeOpenAlex()
    yield fake
    await fake.client.aclose()
    # After every test that used it: no request, anywhere, went out without mailto.
    assert all(r.url.params.get("mailto") == FakeOpenAlex.MAILTO for r in fake.requests)


@pytest.fixture(autouse=True)
def _openalex_off(monkeypatch):
    # A real .env value (a developer's own OPENALEX_MAILTO, e.g. for a manual live check) must never
    # leak into a test. Off by default; a test that wants it on sets it back itself (test_ingest.py does).
    monkeypatch.setattr(settings, "openalex_mailto", "")


@pytest.fixture
def pdf_dir(tmp_path, monkeypatch):
    directory = tmp_path / "pdfs"
    monkeypatch.setattr(settings, "pdf_dir", directory)
    return directory


@pytest.fixture
def app(session, arq, pdf_dir):
    """The app bound to the test session. Tests add their own dependency_overrides (e.g. the LLM)."""
    application = create_app()
    application.dependency_overrides[get_session] = lambda: session
    application.state.arq = arq
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip().split("\n\n"):
        lines = [line for line in block.split("\n") if not line.startswith(":")]  # ": ping" keepalives
        name = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = "\n".join(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append((name, json.loads(data)))
    return events


@pytest.fixture
def answers_in_test_transaction(session, monkeypatch):
    """The answer is saved in a fresh session after the stream; keep it inside the test transaction."""

    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(chat_api, "SessionLocal", test_session)


@pytest.fixture
def fake_llm(app, answers_in_test_transaction):
    """Every chat question (or note-suggestion generation) answered by this FakeLLM, whatever model it names."""
    fake = FakeLLM()
    app.dependency_overrides[resolve_llm] = lambda: fake
    app.dependency_overrides[resolve_llm_by_model_id] = lambda: fake
    return fake


class FakeProvider:
    """Model providers (Ollama, OpenAI-compatible servers) behind httpx.MockTransport, for routes that call one.

    `reply(path, status, **response_kwargs)` answers every request to that path; `refuse(path)` raises ConnectError
    like a server that isn't running. An unrouted request fails loudly. Every request is recorded.
    """

    def __init__(self):
        self.replies: dict[str, tuple[int, dict] | None] = {}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def reply(self, path: str, status: int, **response_kwargs) -> None:
        self.replies[path] = (status, response_kwargs)

    def refuse(self, path: str) -> None:
        self.replies[path] = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path not in self.replies:
            raise AssertionError(f"unrouted provider request: {request.method} {request.url}")
        reply = self.replies[request.url.path]
        if reply is None:
            raise httpx.ConnectError("connection refused", request=request)
        status, kwargs = reply
        return httpx.Response(status, **kwargs)


@pytest.fixture
def provider(app):
    fake = FakeProvider()
    app.dependency_overrides[get_transport] = lambda: fake.transport
    return fake


@dataclass
class DiscoveryFakes:
    openalex: FakeOpenAlex
    s2: FakeProvider
    pdf_host: FakeProvider
    crossref: FakeProvider
    arxiv: FakeProvider
    core: FakeProvider
    unpaywall: FakeProvider
    providers: discovery.Providers  # OpenAlex, Semantic Scholar and the PDF host; the other sources off
    off_clients: dict[str, httpx.AsyncClient]  # built but not yet attached to `providers`

    def turned_on(self, *sources: str) -> discovery.Providers:
        """`providers` with these sources on too: "crossref", "arxiv", "core", "unpaywall"."""
        return replace(self.providers, clients={**self.providers.clients, **{s: self.off_clients[s] for s in sources}})


@pytest.fixture
async def discovery_fakes(fake_openalex):
    """Every paper source and a PDF host behind MockTransport, as the Providers one request uses: OpenAlex routed like
    fake_openalex, the rest FakeProviders (routed by path; an unrouted request fails). `providers` has only OpenAlex,
    Semantic Scholar and the PDF host on, so a test asks exactly the sources it routes; `turned_on` adds others."""
    s2, pdf_host, crossref_host, arxiv_host, core_host, unpaywall_host = (FakeProvider() for _ in range(6))
    off_clients = {
        "crossref": crossref.new_client(email=FakeOpenAlex.MAILTO, transport=crossref_host.transport),
        "arxiv": arxiv.new_client(transport=arxiv_host.transport),
        "core": core_ac.new_client(api_key=None, transport=core_host.transport),
        "unpaywall": unpaywall.new_client(email=FakeOpenAlex.MAILTO, transport=unpaywall_host.transport),
    }
    providers = discovery.Providers(
        clients={
            "openalex": fake_openalex.client,
            "semantic_scholar": semantic_scholar.new_client(api_key=None, transport=s2.transport),
        },
        pdf=httpx.AsyncClient(transport=pdf_host.transport, follow_redirects=True),
    )
    yield DiscoveryFakes(
        fake_openalex, s2, pdf_host, crossref_host, arxiv_host, core_host, unpaywall_host, providers, off_clients
    )
    for client in (providers.pdf, *providers.clients.values(), *off_clients.values()):
        await client.aclose()


@pytest.fixture(autouse=True)
def _real_providers(monkeypatch):
    # A shell that exported LLM_PROVIDER=fake for the E2E stack must not turn provider tests into fake ones.
    monkeypatch.setattr(settings, "llm_provider", "ollama")


@pytest.fixture(autouse=True)
def _test_embed_model(monkeypatch):
    # Test chunks record embed_model "test", and chat refuses papers whose vectors came from another model.
    monkeypatch.setattr(settings, "embed_model", "test")


def _write_pdf(path: Path, pages: list[list[tuple]], metadata: dict[str, str] | None = None) -> Path:
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page()
        for kind, *args in items:
            if kind == "text":
                point, text, size, bold = args
                page.insert_text(point, text, fontsize=size, fontname="hebo" if bold else "helv")
            elif kind == "box":
                rect, text = args
                page.insert_textbox(pymupdf.Rect(*rect), text, fontsize=11)
            elif kind == "rotated":
                point, text = args
                page.insert_text(point, text, fontsize=11, rotate=90)
    if metadata:
        doc.set_metadata(metadata)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def sample_pdf(tmp_path) -> Path:
    """Two pages: a bold 18pt title and a bold section heading, body prose, and an arXiv-style rotated stamp."""
    stamp = ("rotated", (40, 700), "arXiv:1234.5678v1 sidebar stamp")
    return _write_pdf(
        tmp_path / "sample.pdf",
        [
            [("text", (72, 72), "Deep Paper Title", 18, True), ("box", (72, 100, 520, 300), BODY_TEXT), stamp],
            [("text", (72, 72), "2 Method", 11, True), ("box", (72, 100, 520, 300), BODY_TEXT), stamp],
        ],
    )


@pytest.fixture
def titled_pdf(tmp_path) -> Path:
    return _write_pdf(
        tmp_path / "titled.pdf",
        [[("text", (72, 72), "Largest Font Line", 18, True), ("box", (72, 100, 520, 300), BODY_TEXT)]],
        metadata={"title": "Title From Metadata"},
    )


BERT_TITLE_LINES = ("BERT: Pre-training of Deep Bidirectional Transformers for", "Language Understanding")


@pytest.fixture
def arxiv_pdf(tmp_path) -> Path:
    """BERT's first page as arXiv serves it: a two-line title whose second line is its own block (K1), the authors,
    the rotated arXiv stamp, and no DOI or embedded metadata."""
    return _write_pdf(
        tmp_path / "arxiv.pdf",
        [
            [
                ("text", (72, 72), BERT_TITLE_LINES[0], 14, True),
                ("text", (190, 100), BERT_TITLE_LINES[1], 14, True),
                ("text", (72, 130), "Jacob Devlin  Ming-Wei Chang  Kenton Lee  Kristina Toutanova", 11, False),
                ("box", (72, 160, 520, 360), BODY_TEXT),
                ("rotated", (40, 700), "arXiv:1810.04805v2 [cs.CL] 24 May 2019"),
            ]
        ],
    )


@pytest.fixture
def doi_pdf(tmp_path) -> Path:
    """A journal-style first page that prints its DOI, with an embedded author, keywords and creation date."""
    return _write_pdf(
        tmp_path / "doi.pdf",
        [
            [
                ("text", (72, 72), "A Paper With a Printed DOI", 14, True),
                ("box", (72, 100, 520, 300), BODY_TEXT),
                ("text", (72, 760), "https://doi.org/10.18653/v1/N19-1423.", 8, False),
            ]
        ],
        metadata={
            "author": "Jacob Devlin; Ming-Wei Chang",
            "keywords": "language models, pre-training",
            "creationDate": "D:20190528000751Z",
        },
    )


TABLE_ROWS = [("System", "Dev", "Test"), ("BERT-B", "88.5", "87.0"), ("BERT-L", "90.9 ± 0.2", "91.8")]


@pytest.fixture
def table_pdf(tmp_path) -> Path:
    """Page 1: a caption above a 3 x 3 table (columns at x = 72, 220 and 320, rows 14 points apart from y = 150),
    then a paragraph well below it. Every word's box ends inside (60, 135, 420, 185)."""
    table = [
        ("text", (x, 150 + 14 * r), text, 10, r == 0)
        for r, row in enumerate(TABLE_ROWS)
        for x, text in zip((72, 220, 320), row, strict=True)
    ]
    return _write_pdf(
        tmp_path / "table.pdf",
        [
            [
                ("text", (72, 120), "Table 1: Results on the dev set. Higher is better.", 10, False),
                *table,
                ("box", (72, 300, 520, 400), BODY_TEXT),
            ]
        ],
    )


@pytest.fixture
def blank_pdf(tmp_path) -> Path:
    """Stands in for a scanned PDF: a page with no text layer."""
    return _write_pdf(tmp_path / "blank.pdf", [[]])
