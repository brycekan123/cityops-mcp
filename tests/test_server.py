"""
Smoke tests for the cityops MCP server.

Exercises tools, resources, and prompts via FastMCP's in-process Client transport
— no subprocess, no stdio, no network. plan_data_load and the prompts can be
checked offline because they don't hit Open-Meteo. load_weather is exercised
indirectly via plan output only (the real fetch is covered by manual runs).
"""

from __future__ import annotations

import importlib

import pytest
from fastmcp import Client

import cityops_mcp.server as server_mod


def _fresh_server():
    """Reload the server module so its FastMCP instance picks up the test env vars."""
    return importlib.reload(server_mod)


@pytest.mark.asyncio
async def test_server_advertises_all_primitives():
    s = _fresh_server()
    async with Client(s.mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        prompts = await client.list_prompts()

    tool_names = {t.name for t in tools}
    resource_uris = {str(r.uri) for r in resources}
    prompt_names = {p.name for p in prompts}

    assert {"list_sources", "get_loaded_tables", "plan_data_load",
            "check_coverage", "load_weather", "load_csv"}.issubset(tool_names)
    assert {"weather://schema", "weather://tables"}.issubset(resource_uris)
    assert {"extreme_value_query", "trend_overview_query", "specific_date_query",
            "comparison_query", "aggregation_query"}.issubset(prompt_names)


@pytest.mark.asyncio
async def test_plan_data_load_handles_summer_query():
    s = _fresh_server()
    async with Client(s.mcp) as client:
        result = await client.call_tool("plan_data_load",
                                        {"query": "hottest day in atlanta last summer"})

    data = result.structured_content
    assert data["needs_load"] is True
    assert data["args"]["location"] == "Atlanta"
    # Summer = jun 21 to sep 22 of some year
    assert data["args"]["start_date"].endswith("-06-21")
    assert data["args"]["end_date"].endswith("-09-22")


@pytest.mark.asyncio
async def test_plan_data_load_skips_when_no_weather_intent():
    s = _fresh_server()
    async with Client(s.mcp) as client:
        result = await client.call_tool("plan_data_load",
                                        {"query": "what time is it?"})
    assert result.structured_content == {"needs_load": False}


@pytest.mark.asyncio
async def test_check_coverage_returns_false_on_empty_db():
    s = _fresh_server()
    async with Client(s.mcp) as client:
        result = await client.call_tool("check_coverage", {
            "location": "Atlanta",
            "start_date": "2024-06-01",
            "end_date": "2024-06-30",
        })
    assert result.structured_content == {"covered": False}


@pytest.mark.asyncio
async def test_schema_resource_returns_header_even_when_empty():
    s = _fresh_server()
    async with Client(s.mcp) as client:
        contents = await client.read_resource("weather://schema")
    text = contents[0].text
    assert "DATABASE SCHEMA" in text


@pytest.mark.asyncio
async def test_prompts_render_with_arguments():
    s = _fresh_server()
    async with Client(s.mcp) as client:
        result = await client.get_prompt("extreme_value_query", {
            "location": "Atlanta",
            "columns": "location, date, temp_max",
        })
    text = "\n".join(m.content.text for m in result.messages
                     if hasattr(m.content, "text"))
    assert "Atlanta" in text
    assert "temp_max" in text


@pytest.mark.asyncio
async def test_all_prompts_reference_run_sql_tool():
    """Each SQL-generating prompt must tell the model to pass the result to run_sql,
    otherwise the prompts are orphaned (the model has SQL but no executor)."""
    s = _fresh_server()
    prompt_names = ["extreme_value_query", "trend_overview_query", "specific_date_query",
                    "comparison_query", "aggregation_query"]
    async with Client(s.mcp) as client:
        for name in prompt_names:
            result = await client.get_prompt(name, {
                "location": "Atlanta",
                "columns": "location, date, temp_max",
            })
            text = "\n".join(m.content.text for m in result.messages
                             if hasattr(m.content, "text"))
            assert "run_sql" in text, f"prompt '{name}' does not reference run_sql tool"


def test_server_version_matches_package_version():
    """serverInfo.version should report the cityops-mcp package version,
    not the FastMCP framework version."""
    from cityops_mcp import __version__
    s = _fresh_server()
    assert s.mcp.version == __version__
