"""The Connect Claude page's server check: a real `python -m mcp_server` subprocess, as the api starts it."""

import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy.engine import make_url
from test_mcp_server import EXPECTED_TOOLS

from app.core import mcp_check

pytestmark = pytest.mark.anyio

ALL_TOOLS = sorted(EXPECTED_TOOLS)


@pytest.fixture(autouse=True)
def server_runs_as_the_api_starts_it(monkeypatch, tmp_path):
    # The subprocess inherits the environment: the test database (read-only, since it can't join a test's rolled-back
    # transaction), and no PYTHONPATH, as in the api container. It must find mcp_server from any working directory.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(tmp_path)


async def test_the_real_server_answers_with_all_its_tools_and_reads_the_library():
    assert await mcp_check.check_server() == mcp_check.McpCheck(ok=True, tools=ALL_TOOLS, detail=None)


async def test_a_server_that_doesnt_answer_in_time_is_reported():
    result = await mcp_check.check_server(timeout=0.01)

    assert result == mcp_check.McpCheck(ok=False, tools=[], detail="The MCP server didn't answer within 0.01 seconds.")


async def test_a_server_that_cant_start_points_at_the_api_logs(monkeypatch):
    monkeypatch.setattr(mcp_check, "SERVER_MODULE", "no_such_mcp_server")

    result = await mcp_check.check_server()

    assert result == mcp_check.McpCheck(
        ok=False, tools=[], detail="The MCP server couldn't start. Check the api logs: docker compose logs api."
    )


async def test_a_server_missing_tools_names_them(monkeypatch, tmp_path):
    (tmp_path / "half_mcp_server.py").write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "server = MCPServer('half')\n"
        "@server.tool()\n"
        "def get_paper(paper_id: str) -> str:\n"
        "    return paper_id\n"
        "server.run()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(mcp_check, "SERVER_MODULE", "half_mcp_server")

    result = await mcp_check.check_server()

    assert result == mcp_check.McpCheck(
        ok=False,
        tools=["get_paper"],
        detail="The MCP server is missing tools: " + ", ".join(n for n in ALL_TOOLS if n != "get_paper") + ".",
    )


async def test_a_server_that_cant_read_the_library_says_so(monkeypatch):
    missing = make_url(TEST_DATABASE_URL).set(database="paperlab_no_such_database")
    monkeypatch.setenv("DATABASE_URL", missing.render_as_string(hide_password=False))

    result = await mcp_check.check_server()

    assert result == mcp_check.McpCheck(
        ok=False, tools=ALL_TOOLS, detail="The MCP server started but couldn't read the library."
    )
