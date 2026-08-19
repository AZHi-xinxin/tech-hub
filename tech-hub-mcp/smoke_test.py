"""End-to-end smoke test using the official MCP Python client."""

from __future__ import annotations

import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_URL = os.environ.get("TECHHUB_MCP_TEST_URL", "http://127.0.0.1:8793/mcp")
MCP_TOKEN = os.environ.get("TECHHUB_MCP_TOKEN", "").strip()
EXPECTED_TOOLS = {
    "hub_health",
    "list_rooms",
    "read_messages",
    "send_message",
    "create_task",
    "get_task",
}


async def _call_read_only(session: ClientSession, name: str, arguments: dict) -> None:
    result = await session.call_tool(name, arguments)
    if getattr(result, "isError", False):
        raise RuntimeError(f"{name} returned an MCP tool error")


async def main() -> None:
    if len(MCP_TOKEN) < 32:
        raise RuntimeError("TECHHUB_MCP_TOKEN is missing or too short")

    headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
    async with httpx.AsyncClient(headers=headers, timeout=40.0) as http_client:
        async with streamable_http_client(
            MCP_URL,
            http_client=http_client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                missing = EXPECTED_TOOLS - names
                unexpected = names - EXPECTED_TOOLS
                if missing or unexpected:
                    raise RuntimeError(
                        f"unexpected tool catalog; missing={sorted(missing)}, "
                        f"unexpected={sorted(unexpected)}"
                    )
                await _call_read_only(session, "hub_health", {})
                await _call_read_only(session, "list_rooms", {})
                await _call_read_only(
                    session,
                    "read_messages",
                    {"room": "general", "after_seq": 0, "limit": 1},
                )

    print("MCP_SMOKE_OK")
    print("TOOLS=" + ",".join(sorted(EXPECTED_TOOLS)))


if __name__ == "__main__":
    asyncio.run(main())
