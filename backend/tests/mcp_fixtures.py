"""Fixtures for the M39 MCP tool tests: tools use the test's rolled-back session, jobs go to a FakeArq, and paper
sources answer offline."""

from contextlib import asynccontextmanager

import pytest
from conftest import FakeArq
from mcp import Client

from app.core import discovery, queue
from app.core.paper_sources import SourceSettings
from app.core.source_registry import SOURCE_IDS
from app.providers import discovery_fake
from mcp_server import server


@pytest.fixture(autouse=True)
def tools_use_the_test_session(session, monkeypatch):
    @asynccontextmanager
    async def test_session():
        yield session

    monkeypatch.setattr(server, "sessions", test_session)


@pytest.fixture(autouse=True)
def jobs(monkeypatch) -> FakeArq:
    pool = FakeArq()
    monkeypatch.setattr(queue, "_pool", pool)
    return pool


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    """Every paper source on, routed to discovery_fake's offline MockTransport."""
    every_source = SourceSettings(contact_email=discovery_fake.MAILTO, enabled=dict.fromkeys(SOURCE_IDS, True))

    @asynccontextmanager
    async def offline(_session):
        providers = discovery.build_providers(every_source, discovery_fake.transport())
        try:
            yield providers
        finally:
            await providers.aclose()

    monkeypatch.setattr(discovery, "open_providers", offline)


async def call(tool: str, **arguments):
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments)


def result(response):
    assert not response.is_error, response.structured_content or response.content
    content = response.structured_content
    return content["result"] if "result" in content else content
