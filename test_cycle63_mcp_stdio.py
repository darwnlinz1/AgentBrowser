"""Cycle 63: real MCP protocol over stdio (SDK drift guard).

Spawns the actual server subprocess and speaks MCP with the official
client: initialize, tools/list (schema shape from explicit signatures),
and a session_status call that needs no browser. If an SDK major renames
or re-schema-izes tools again, this fails instead of the dispatcher
unit tests passing against a dead transport.
"""

import json
import os
import sys

import pytest

pytest.importorskip("mcp")

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))


@pytest.mark.asyncio
async def test_mcp_stdio_tools_and_status():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_browser.mcp_server"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "session_status" in names
            assert "debug_episode" in names
            assert "perf_vitals" in names
            assert "eval" not in names and "shutdown" not in names
            assert len(names) >= 20

            by_name = {t.name: t for t in tools.tools}
            tool = by_name["debug_episode"]
            episode_schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
            if not isinstance(episode_schema, dict):
                episode_schema = episode_schema.model_dump(by_alias=True)
            assert "url" in (episode_schema.get("properties") or {})
            assert episode_schema.get("required") == ["url"]

            result = await session.call_tool("session_status", {})
            assert not (getattr(result, "isError", None) or getattr(result, "is_error", False))
            payload = json.loads(result.content[0].text)
            assert payload["active"] is False
            assert payload["sessions"] == []
