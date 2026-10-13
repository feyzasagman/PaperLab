"""The real launch: `python -m mcp_server` over stdio, with the initialize handshake Claude Desktop uses (M6, D89)."""

import logging
import os
import sys
import uuid
from pathlib import Path

import pytest
from conftest import TEST_DATABASE_URL
from mcp import Client, StdioServerParameters
from test_mcp_server import EXPECTED_TOOLS

pytestmark = pytest.mark.anyio

BACKEND = Path(__file__).resolve().parents[1]


async def test_the_server_starts_on_stdio_and_answers_a_tool_call(caplog):
    # Read-only on purpose: a separate process can't join a test's rolled-back transaction. An unknown workspace is
    # refused before anything is embedded, so no model loads either.
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        cwd=BACKEND,
    )
    caplog.set_level(logging.ERROR, logger="mcp.client.stdio")

    async with Client(server, mode="legacy", read_timeout_seconds=60) as client:
        handshake = client.protocol_version
        tools = sorted(tool.name for tool in (await client.list_tools()).tools)
        unknown = await client.call_tool(
            "search_library", {"query": "anything", "workspace": f"No such workspace {uuid.uuid4()}"}
        )

    assert handshake == "2025-11-25"
    assert tools == sorted(EXPECTED_TOOLS)
    assert unknown.is_error and unknown.structured_content["error"] == "unknown_workspace"
    assert isinstance(unknown.structured_content["available"], list)
    # The client skips a stdout line that isn't the protocol (a stray print in the server) and only logs it.
    assert [record.getMessage() for record in caplog.records if record.name == "mcp.client.stdio"] == []
