import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import FakeEmbedder, unit_vector
from sqlalchemy import select, update

from app.config import settings
from app.core import embedding_index, embedding_sources, llm_connections
from app.core.errors import Conflict
from app.models import Chunk, Note, Paper
from app.providers import embedding
from app.workers import ingest
from app.workers.settings import IngestWorkerSettings

pytestmark = pytest.mark.anyio


async def indexed_paper(session, model: str = "test", chunks: int = 2, embedded: bool = True) -> Paper:
    paper = Paper(title=f"Indexed {uuid.uuid4().hex[:6]}", file_path="/nonexistent.pdf", status="ready", page_count=1)
    session.add(paper)
    await session.flush()
    session.add_all(
        Chunk(
            paper_id=paper.id, ordinal=i, page=1, bbox=[[72, 100, 300, 120]], text=f"{paper.title} passage {i}",
            embedding=unit_vector(f"{paper.title} {i}") if embedded else None, embed_model=model, strategy_ver=1,
        )
        for i in range(chunks)
    )
    await session.commit()
    return paper


OLLAMA_NAME = "ollama/nomic-embed-text"


def worker_uses(session, monkeypatch) -> None:
    """The worker's jobs open this test's rolled-back session instead of their own."""

    @asynccontextmanager
    async def shared_session():
        yield session

    monkeypatch.setattr(ingest, "SessionLocal", shared_session)


async def vectors_of(session, paper_id) -> list[tuple[str, list[float] | None]]:
    # populate_existing: set_embeddings is a bulk UPDATE, which leaves loaded chunks stale.
    query = select(Chunk).where(Chunk.paper_id == paper_id).order_by(Chunk.ordinal)
    chunks = await session.scalars(query.execution_options(populate_existing=True))
    return [(c.embed_model, None if c.embedding is None else list(c.embedding)) for c in chunks]


async def ollama_source(session) -> embedding_sources.Source:
    label = f"Ollama {uuid.uuid4().hex[:8]}"
    connection = await llm_connections.create_connection(session, "ollama", label, "http://ollama.test:11434", None)
    return await embedding_sources.candidate(session, "ollama", connection.id, None)


class SwitchingEmbedder(FakeEmbedder):
    """Embeds, while the owner switches search to another source."""

    def __init__(self, session, switch_to: embedding_sources.Source):
        super().__init__()
        self._session, self._switch_to = session, switch_to

    async def encode(self, texts):
        await embedding_sources.save(self._session, self._switch_to)
        return await super().encode(texts)


async def test_status_counts_vectors_by_the_model_they_came_from(session):
    before = await embedding_index.status(session, "test")
    await indexed_paper(session, "old-model", chunks=3)
    await indexed_paper(session, "test", chunks=2)
    await indexed_paper(session, "test", chunks=4, embedded=False)

    after = await embedding_index.status(session, "test")

    def counts(status):
        return {i.model: i.chunks for i in status.indexed_with}

    assert after.model == "test"
    assert after.chunks - before.chunks == 5
    assert counts(after).get("old-model", 0) - counts(before).get("old-model", 0) == 3
    assert counts(after).get("test", 0) - counts(before).get("test", 0) == 2
    assert [i.chunks for i in after.indexed_with] == sorted((i.chunks for i in after.indexed_with), reverse=True)


async def test_unembedded_papers_are_those_with_chunks_and_no_vector_whatever_their_status(session):
    none = await indexed_paper(session, embedded=False)
    mid_ingest = await indexed_paper(session, embedded=False)
    mid_ingest.status = "enriching"
    embedded = await indexed_paper(session)
    no_chunks = await indexed_paper(session, chunks=0)
    await session.commit()

    found = set(await embedding_index.unembedded_papers(session))

    assert {none.id, mid_ingest.id} <= found
    assert not {embedded.id, no_chunks.id} & found


async def test_vectors_from_another_model_are_refused_and_unembedded_chunks_are_not(session):
    old = await indexed_paper(session, "old-model")
    current = await indexed_paper(session, "test")
    pending = await indexed_paper(session, "old-model", embedded=False)

    with pytest.raises(Conflict, match="^embedding_model_changed$"):
        await embedding_index.check_model(session, "test", [current.id, old.id])
    await embedding_index.check_model(session, "test", [current.id, pending.id])
    await embedding_index.check_model(session, "old-model", [old.id])


@pytest.mark.parametrize("scope", ["paper", "workspace"])
async def test_chat_is_refused_when_the_embedding_model_changed(
    client, session, fake_llm, embedder, monkeypatch, scope
):
    monkeypatch.setattr(embedding, "get_model", lambda: embedder)
    monkeypatch.setattr("app.core.chat.SMALL_PAPER_CHARS", 0)  # retrieve even for this short paper
    paper = await indexed_paper(session, "nomic-ai/nomic-embed-text-v1.5")
    monkeypatch.setattr(settings, "embed_model", "BAAI/bge-base-en-v1.5")
    created = await client.post("/api/workspaces", json={"name": f"Embedding {uuid.uuid4().hex[:6]}"})
    workspace_id = created.json()["id"]
    await client.put(f"/api/workspaces/{workspace_id}/papers/{paper.id}")
    path = f"/api/papers/{paper.id}/chat" if scope == "paper" else f"/api/workspaces/{workspace_id}/chat"

    response = await client.post(path, json={"question": "why?"})

    assert (response.status_code, response.json()) == (409, {"detail": "embedding_model_changed"})
    assert (fake_llm.calls, embedder.calls) == ([], [])


async def test_status_route(client, session):
    await indexed_paper(session, "test", chunks=2)

    body = (await client.get("/api/embedding")).json()

    assert body["model"] == "test"
    assert body["chunks"] == sum(i["chunks"] for i in body["indexed_with"])
    assert "test" in [i["model"] for i in body["indexed_with"]]


async def test_reindexing_needs_the_explicit_confirmation_then_queues_every_paper(client, session, arq):
    papers = [await indexed_paper(session, "old-model"), await indexed_paper(session, "test", embedded=False)]

    for body in [{}, {"confirm": False}, {"confirm": "yes"}]:
        assert (await client.post("/api/embedding/reindex", json=body)).status_code == 422
    assert arq.jobs == []

    response = await client.post("/api/embedding/reindex", json={"confirm": True})

    every_paper = set(await session.scalars(select(Chunk.paper_id).distinct()))
    assert response.status_code == 202
    assert response.json() == {"papers": len(every_paper)}
    assert set(arq.jobs) == {("reembed_paper", str(paper_id), False) for paper_id in every_paper}
    assert {str(p.id) for p in papers} <= {paper_id for _, paper_id, _ in arq.jobs}


async def test_reembed_paper_embeds_every_chunk_again_with_the_configured_model_in_place(
    session, embedder, monkeypatch
):
    paper = await indexed_paper(session, "old-model", chunks=3)
    before = {c.id: c.embedding for c in await session.scalars(select(Chunk).where(Chunk.paper_id == paper.id))}

    @asynccontextmanager
    async def shared_session():
        yield session

    monkeypatch.setattr(ingest, "SessionLocal", shared_session)
    await ingest.reembed_paper({"embedder": embedder}, str(paper.id))

    query = select(Chunk).where(Chunk.paper_id == paper.id).order_by(Chunk.ordinal)
    after = list(await session.scalars(query.execution_options(populate_existing=True)))
    assert {c.id for c in after} == set(before)  # the same chunks: answers and notes keep their sources
    assert [c.embed_model for c in after] == ["test"] * 3
    assert embedder.calls[0][0] == [f"search_document: {paper.title} passage {i}" for i in range(3)]
    assert all(list(c.embedding) != list(before[c.id]) for c in after)
    await embedding_index.check_model(session, "test", [paper.id])
    assert ingest.reembed_paper in IngestWorkerSettings.functions  # one paper at a time (2026-10-13)


async def test_papers_to_embed_are_those_with_a_chunk_not_on_that_source(session):
    done = await indexed_paper(session, "test")
    other = await indexed_paper(session, "old-model")
    unembedded = await indexed_paper(session, "test", embedded=False)
    half = await indexed_paper(session, "test")
    await session.execute(update(Chunk).where(Chunk.paper_id == half.id, Chunk.ordinal == 0).values(embedding=None))

    found = set(await embedding_index.papers_to_embed(session, "test"))

    assert {other.id, unembedded.id, half.id} <= found and done.id not in found
    assert await embedding_index.papers_to_embed(session, "test", [done.id, other.id]) == [other.id]


async def test_a_missing_only_job_skips_a_paper_already_on_the_active_source(session, embedder, monkeypatch):
    done, waiting = await indexed_paper(session, "test"), await indexed_paper(session, "old-model")
    worker_uses(session, monkeypatch)

    await ingest.reembed_paper({"embedder": embedder}, str(done.id), True)
    await ingest.reembed_paper({"embedder": embedder}, str(waiting.id), True)

    waiting_texts = [f"search_document: {waiting.title} passage {i}" for i in range(2)]
    assert [texts for texts, _ in embedder.calls] == [waiting_texts]
    assert {model for model, _ in await vectors_of(session, waiting.id)} == {"test"}


async def test_reembed_keeps_the_old_vectors_when_the_source_fails_and_records_why(session, monkeypatch):
    paper = await indexed_paper(session, "old-model")
    before = await vectors_of(session, paper.id)
    worker_uses(session, monkeypatch)
    refusing = FakeEmbedder(label="OpenAI", refuse="Key rejected by OpenAI")

    await ingest.reembed_paper({"embedder": refusing}, str(paper.id))

    assert await vectors_of(session, paper.id) == before
    assert (await embedding_sources.active(session)).error == "Key rejected by OpenAI"


async def test_vectors_record_the_name_of_the_source_that_made_them(session, monkeypatch):
    source = await ollama_source(session)
    await embedding_sources.save(session, source)
    paper = await indexed_paper(session, "test")
    worker_uses(session, monkeypatch)

    await ingest.reembed_paper({"embedder": FakeEmbedder(name=OLLAMA_NAME, label=source.label)}, str(paper.id))

    assert {model for model, _ in await vectors_of(session, paper.id)} == {OLLAMA_NAME}


async def test_a_switch_while_embedding_drops_the_vectors_and_says_so(session, monkeypatch, caplog):
    paper = await indexed_paper(session, "test", embedded=False)
    switching = SwitchingEmbedder(session, await ollama_source(session))
    worker_uses(session, monkeypatch)

    with caplog.at_level("INFO", logger="app.workers.ingest"):
        await ingest.reembed_paper({"embedder": switching}, str(paper.id))

    assert switching.calls  # it did embed
    assert all(vector is None for _, vector in await vectors_of(session, paper.id))
    assert f"the search source changed while embedding {paper.id}; the newer job embeds it" in caplog.text


async def test_the_library_size_is_its_papers_with_chunks_its_notes_and_their_characters(session):
    before = await embedding_index.library_size(session)
    paper = await indexed_paper(session, chunks=3)
    note = Note(body="notes go to a cloud search source too (P3 = A)", provenance="human")
    session.add(note)
    await session.commit()
    chunk_chars = sum(len(c.text) for c in await session.scalars(select(Chunk).where(Chunk.paper_id == paper.id)))

    after = await embedding_index.library_size(session)

    assert (after.papers - before.papers, after.notes - before.notes, after.chars - before.chars) == (
        1, 1, chunk_chars + len(note.body)
    )  # fmt: skip
