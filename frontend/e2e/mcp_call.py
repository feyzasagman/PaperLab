"""Calls one PaperLab MCP tool the way Claude Desktop does: it starts `python -m mcp_server` and talks to it over stdio
with the initialize handshake. Prints {"is_error": …, "result": …} as one JSON line. For E2E specs only.

Run inside the api container, which has the server and its database (D89):
  scripts/e2e-stack.sh exec api python - <tool> '<arguments as JSON>' < frontend/e2e/mcp_call.py
"""

import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters


async def main(tool: str, arguments: str) -> None:
    # env: the SDK otherwise passes the server only a few safe variables, and the server needs DATABASE_URL and friends.
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"], env=dict(os.environ))
    async with Client(server, mode="legacy", read_timeout_seconds=60) as client:
        result = await client.call_tool(tool, json.loads(arguments))
    print(json.dumps({"is_error": result.is_error, "result": result.structured_content}))


asyncio.run(main(*sys.argv[1:]))
