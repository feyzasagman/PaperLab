"""Checks PaperLab's side of an MCP connection, for the Connect Claude page: starts `python -m mcp_server` the way a
client does, over stdio, and reads the library through it. It can't see a client's own config."""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

BACKEND = Path(__file__).resolve().parents[2]
SERVER_MODULE = "mcp_server"  # tests point this at a module that doesn't exist
TOOLS = [
    "accept_suggestions",
    "add_paper",
    "add_paper_to_workspace",
    "create_note",
    "create_saved_search",
    "create_workspace",
    "get_paper",
    "get_prisma",
    "get_screening_suggestions",
    "get_search_run",
    "import_hit",
    "list_hits",
    "list_notes",
    "list_papers",
    "list_saved_searches",
    "list_workspaces",
    "related_papers",
    "remove_paper_from_workspace",
    "review_hits",
    "run_prescreen",
    "search_library",
    "set_eligibility",
    "set_saved_search_enabled",
    "snowball",
    "start_search_run",
    "stop_search_run",
    "verify_claim",
    "verify_claims",
]
# No paper has this id, so get_paper reads the database and answers not_found, without loading the embedding model.
NO_PAPER = "00000000-0000-4000-8000-000000000000"
logger = logging.getLogger(__name__)
COULD_NOT_START = "The MCP server couldn't start. Check the api logs: docker compose logs api."


@dataclass(frozen=True)
class McpCheck:
    ok: bool
    tools: list[str]
    detail: str | None


async def check_server(*, timeout: float = 30) -> McpCheck:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", SERVER_MODULE], env=dict(os.environ), cwd=BACKEND
    )
    tools: list[str] = []
    try:
        with anyio.fail_after(timeout):
            async with Client(server, mode="legacy") as client:
                tools = sorted(tool.name for tool in (await client.list_tools()).tools)
                if tools != TOOLS:
                    missing = ", ".join(name for name in TOOLS if name not in tools)
                    return McpCheck(ok=False, tools=tools, detail=f"The MCP server is missing tools: {missing}.")
                answer = await client.call_tool("get_paper", {"paper_id": NO_PAPER})
    except TimeoutError:
        return McpCheck(ok=False, tools=tools, detail=f"The MCP server didn't answer within {timeout:g} seconds.")
    except Exception:
        logger.exception("The MCP server check failed")
        return McpCheck(ok=False, tools=tools, detail=COULD_NOT_START)
    if not (answer.is_error and (answer.structured_content or {}).get("error") == "not_found"):
        return McpCheck(ok=False, tools=tools, detail="The MCP server started but couldn't read the library.")
    return McpCheck(ok=True, tools=tools, detail=None)
