"""queue.enqueue and discovery.open_providers: jobs and paper sources without a FastAPI request (M39 F1)."""

import pytest
from conftest import FakeArq

from app.config import settings
from app.core import discovery, queue
from app.providers import discovery_fake

pytestmark = pytest.mark.anyio


async def test_enqueue_sends_the_job_to_the_pool(monkeypatch):
    pool = FakeArq()
    monkeypatch.setattr(queue, "_pool", pool)

    await queue.enqueue("ingest_paper", "abc")

    assert pool.jobs == [("ingest_paper", "abc")]


async def test_open_providers_in_fake_mode_answers_offline_and_closes(session, monkeypatch):
    monkeypatch.setattr(settings, "discovery_provider", "fake")

    async with discovery.open_providers(session) as providers:
        found = await discovery.search(session, providers, f"{discovery_fake.DOI_PREFIX}free")
        pdf = providers.pdf

    assert any(c.doi == f"{discovery_fake.DOI_PREFIX}free" for c in found.results)
    assert pdf.is_closed
