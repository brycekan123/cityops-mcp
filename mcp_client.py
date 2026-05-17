"""
Sync MCP client wrapper.

Connects to data_server.py via stdio (spawned once as a subprocess).
All public functions are synchronous so LangGraph nodes can call them directly.
Every call prints a [MCP] trace line.
"""

import asyncio
import json
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

SERVER_SCRIPT = Path(__file__).parent / "city_data_server.py"


def _transport() -> PythonStdioTransport:
    return PythonStdioTransport(script_path=SERVER_SCRIPT, keep_alive=True)


def _run(coro):
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _extract(result) -> dict:
    """Pull structured content from a CallToolResult."""
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    for block in result.content:
        if hasattr(block, "text"):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"text": block.text}
    return {}


def _fmt(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"
    if "files" in result:
        return str(result["files"])
    if "preview_rows" in result:
        cols = result.get("columns", [])
        n    = len(result.get("preview_rows", []))
        return f"{len(cols)} columns: {', '.join(cols)} | {n} preview rows shown"
    if "table_name" in result:
        dr = f" [{result['date_range']}]" if result.get("date_range") else ""
        return f"loaded {result['row_count']} rows → table '{result['table_name']}'{dr}"
    return str(result)


# ── Public API ────────────────────────────────────────────────────────────────

def list_mcp_tools() -> list[str]:
    """Connect to the MCP server and return available tool names."""
    print(f"[MCP] connecting to {SERVER_SCRIPT.name} via stdio...")

    async def _call():
        async with Client(_transport()) as client:
            tools = await client.list_tools()
            return [t.name for t in tools]

    names = _run(_call())
    print(f"[MCP] list_tools   → {names}")
    return names


def call_tool(tool_name: str, args: dict) -> dict:
    """Call a named tool on the MCP server and return its result as a dict."""
    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"[MCP] calling tool → {tool_name}({arg_str})")

    async def _call():
        async with Client(_transport()) as client:
            return await client.call_tool(tool_name, args)

    raw    = _run(_call())
    result = _extract(raw)
    print(f"[MCP] response     → {_fmt(result)}")
    return result
