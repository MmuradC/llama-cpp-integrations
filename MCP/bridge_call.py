#!/usr/bin/env python3
"""Call one tool on one MCP server through the mcp-bridge (port 9000).

The bridge's SSE transport requires the client to hold the GET /sse stream
open for the whole session — initialize's response, and every later
response, are delivered over that same stream, not the POST. A client that
grabs the session_id from the first SSE event and then disconnects (e.g. a
one-shot curl per request) will see every request rejected with
"Received request before initialization was complete", because the
session's initialize handshake never actually completes. This script uses
the mcp SDK's own sse_client/ClientSession, which manages that correctly
(same pattern already used by panel/backend/server.py's _check_mcp_server).

Usage:
    bridge_call.py <server_name> <tool_name> ['<json_args>']
    bridge_call.py rag rag_query '{"collection": "5G Positioning", "question": "..."}'
    bridge_call.py filesystem list_allowed_directories
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client

BRIDGE_URL = os.environ.get("MCP_BRIDGE_URL", "http://127.0.0.1:9000").rstrip("/")


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    server_name, tool_name = sys.argv[1], sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    url = f"{BRIDGE_URL}/servers/{server_name}/sse"
    async with asyncio.timeout(60):
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
    for block in result.content:
        text = getattr(block, "text", None)
        print(text if text is not None else block)
    if result.isError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
