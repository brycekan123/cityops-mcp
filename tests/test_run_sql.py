"""Tests for the run_sql MCP tool — read-only query execution against the cityops DB."""

from __future__ import annotations

import importlib
import sqlite3

import pytest
from fastmcp import Client

import cityops_mcp.server as server_mod
from cityops_mcp.paths import get_db_path


def _fresh_server():
    return importlib.reload(server_mod)


def _seed_weather_rows(rows: list[tuple]) -> None:
    """Create weather_daily and insert (location, date, temp_max, temp_min, precip_mm, wind_mph) rows."""
    conn = sqlite3.connect(str(get_db_path()))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_daily (
            location  TEXT,
            date      TEXT,
            temp_max  REAL,
            temp_min  REAL,
            precip_mm REAL,
            wind_mph  REAL,
            loaded_at TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO weather_daily (location, date, temp_max, temp_min, precip_mm, wind_mph, loaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-05-19T00:00:00')",
        rows,
    )
    conn.commit()
    conn.close()


async def _call_run_sql(query: str):
    s = _fresh_server()
    async with Client(s.mcp) as client:
        return await client.call_tool("run_sql", {"query": query}, raise_on_error=False)


@pytest.mark.asyncio
async def test_run_sql_happy_path_returns_rows():
    _seed_weather_rows([
        ("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0),
        ("Atlanta", "2024-07-02", 97.0, 76.0, 0.0, 6.0),
        ("Atlanta", "2024-07-03", 99.0, 78.0, 0.0, 7.0),
    ])
    result = await _call_run_sql(
        "SELECT date, temp_max FROM weather_daily ORDER BY date"
    )
    data = result.structured_content
    assert data["columns"] == ["date", "temp_max"]
    assert data["row_count"] == 3
    assert data["truncated"] is False
    assert data["rows"][0] == ["2024-07-01", 95.0]
    assert data["rows"][2] == ["2024-07-03", 99.0]


@pytest.mark.asyncio
async def test_run_sql_empty_result_is_not_an_error():
    _seed_weather_rows([("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0)])
    result = await _call_run_sql(
        "SELECT * FROM weather_daily WHERE location = 'Nowhere'"
    )
    data = result.structured_content
    assert data["row_count"] == 0
    assert data["rows"] == []
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_run_sql_truncates_at_1000_rows():
    big_rows = [
        ("Atlanta", f"2024-{(i // 31) + 1:02d}-{(i % 31) + 1:02d}", float(i), 0.0, 0.0, 0.0)
        for i in range(1100)
    ]
    _seed_weather_rows(big_rows)
    result = await _call_run_sql("SELECT * FROM weather_daily")
    data = result.structured_content
    assert data["truncated"] is True
    assert data["row_count"] == 1000
    assert len(data["rows"]) == 1000


@pytest.mark.asyncio
async def test_run_sql_rejects_insert():
    result = await _call_run_sql(
        "INSERT INTO weather_daily (location) VALUES ('x')"
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_update():
    _seed_weather_rows([("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0)])
    result = await _call_run_sql("UPDATE weather_daily SET temp_max = 0")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_delete():
    _seed_weather_rows([("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0)])
    result = await _call_run_sql("DELETE FROM weather_daily")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_drop():
    _seed_weather_rows([("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0)])
    result = await _call_run_sql("DROP TABLE weather_daily")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_attach():
    result = await _call_run_sql("ATTACH DATABASE '/tmp/x.db' AS x")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_pragma_write():
    result = await _call_run_sql("PRAGMA journal_mode = WAL")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_multi_statement():
    _seed_weather_rows([("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0)])
    result = await _call_run_sql(
        "SELECT 1; DROP TABLE weather_daily"
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_rejects_empty_query():
    result = await _call_run_sql("")
    assert result.is_error is True

    result = await _call_run_sql("   \n  ")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_surfaces_sqlite_error_for_bad_column():
    _seed_weather_rows([("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0)])
    result = await _call_run_sql("SELECT nonsense_col FROM weather_daily")
    assert result.is_error is True


@pytest.mark.asyncio
async def test_run_sql_with_cte_is_allowed():
    _seed_weather_rows([
        ("Atlanta", "2024-07-01", 95.0, 75.0, 0.1, 8.0),
        ("Atlanta", "2024-07-02", 97.0, 76.0, 0.0, 6.0),
    ])
    result = await _call_run_sql(
        "WITH atl AS (SELECT * FROM weather_daily WHERE location='Atlanta') "
        "SELECT MAX(temp_max) FROM atl"
    )
    data = result.structured_content
    assert data["row_count"] == 1
    assert data["rows"][0][0] == 97.0


def test_read_only_authorizer_denies_writes():
    """Authorizer must deny INSERT/UPDATE/DELETE/DROP/CREATE/ATTACH directly,
    independent of the layer-1 string validation."""
    from cityops_mcp.server import _read_only_authorizer

    DENY = sqlite3.SQLITE_DENY
    # First positional arg of the authorizer callback is the action code.
    assert _read_only_authorizer(sqlite3.SQLITE_INSERT, "weather_daily", None, "main", None) == DENY
    assert _read_only_authorizer(sqlite3.SQLITE_UPDATE, "weather_daily", "temp_max", "main", None) == DENY
    assert _read_only_authorizer(sqlite3.SQLITE_DELETE, "weather_daily", None, "main", None) == DENY
    assert _read_only_authorizer(sqlite3.SQLITE_DROP_TABLE, "weather_daily", None, "main", None) == DENY
    assert _read_only_authorizer(sqlite3.SQLITE_CREATE_TABLE, "x", None, "main", None) == DENY
    assert _read_only_authorizer(sqlite3.SQLITE_ATTACH, "/tmp/x.db", None, None, None) == DENY


def test_read_only_authorizer_allows_reads():
    from cityops_mcp.server import _read_only_authorizer

    OK = sqlite3.SQLITE_OK
    assert _read_only_authorizer(sqlite3.SQLITE_SELECT, None, None, None, None) == OK
    assert _read_only_authorizer(sqlite3.SQLITE_READ, "weather_daily", "temp_max", "main", None) == OK
    assert _read_only_authorizer(sqlite3.SQLITE_FUNCTION, None, "MAX", None, None) == OK
    assert _read_only_authorizer(sqlite3.SQLITE_RECURSIVE, None, None, None, None) == OK
