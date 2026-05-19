"""
cityops-mcp — MCP server for city operational data.

Exposes weather data tools (Open-Meteo forecast + historical archive) and local CSVs.
Writes directly to a session-scoped SQLite database; returns lightweight metadata to
the MCP client.

Run as a module:  python -m cityops_mcp
Run as a script:  cityops-mcp        (installed via pyproject.toml entry point)
"""

from __future__ import annotations

import calendar
import csv
import logging
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from cityops_mcp import __version__
from cityops_mcp.data_sources import fetch_weather
from cityops_mcp.paths import get_csv_dir, get_db_path


def _configure_logging() -> logging.Logger:
    """
    Configure stderr-only logging — stdout is reserved for MCP JSON-RPC.
    Level controlled by CITYOPS_LOG_LEVEL (default INFO).
    """
    level_name = os.environ.get("CITYOPS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
    ))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger("cityops_mcp")


_logger = _configure_logging()


# ── Keyword tables ────────────────────────────────────────────────────────────

_WEATHER_KEYWORDS = {
    "weather", "forecast", "temperature", "temp", "rain", "rainy", "precipitation",
    "snow", "wind", "humid", "sunny", "cloudy", "hot", "cold", "warm", "cool",
    "hottest", "coldest", "warmest", "coolest", "degrees", "climate", "conditions",
}
_TIME_KEYWORDS = {
    "today", "tomorrow", "yesterday", "week", "month", "year",
    "summer", "winter", "spring", "fall", "autumn",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
# Longest names first so "new york" matches before "new"
_SUPPORTED_CITIES = [
    "new york", "los angeles", "chicago", "houston", "seattle", "miami", "denver", "atlanta",
]
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_MONTH_YEAR_RE = {
    name: re.compile(rf"{name}\s+(\d{{4}})") for name in _MONTH_NAMES
}
_NAMED_DAY_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:of\s+)?(\d{4}))?"
)


def _extract_city(query: str) -> str:
    q = query.lower()
    for city in _SUPPORTED_CITIES:
        if city in q:
            return city.title()
    return "Atlanta"


# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "cityops-mcp",
    version=__version__,
    middleware=[
        ErrorHandlingMiddleware(logger=_logger, include_traceback=False),
        TimingMiddleware(logger=_logger, log_level=logging.DEBUG),
    ],
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(get_db_path()))


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    """Return user-visible tables (everything except the internal source_loads ledger)."""
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT IN ('source_loads')"
    ).fetchall()]


def _ensure_source_loads(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_loads (
            source_name  TEXT,
            table_name   TEXT,
            loaded_at    TEXT,
            row_count    INTEGER,
            query_params TEXT
        )
    """)


def _insert_rows(conn: sqlite3.Connection, table_name: str, rows: list[dict],
                 source: str, params: str = "") -> list[str]:
    if not rows:
        return []
    columns = list(rows[0].keys())
    col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')
    ph = ", ".join("?" for _ in columns)
    conn.executemany(
        f'INSERT INTO "{table_name}" VALUES ({ph})',
        [[str(row.get(c, "")) for c in columns] for row in rows],
    )
    _ensure_source_loads(conn)
    conn.execute(
        "INSERT INTO source_loads VALUES (?, ?, ?, ?, ?)",
        (source, table_name, date.today().isoformat(), len(rows), params)
    )
    conn.commit()
    return columns


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("weather://schema", mime_type="text/plain")
def schema_resource() -> str:
    """Current database schema — all tables and their columns."""
    with _conn() as conn:
        lines = ["=== DATABASE SCHEMA ==="]
        for tbl in _user_tables(conn):
            cols = conn.execute(f"PRAGMA table_info('{tbl}')").fetchall()
            col_parts = [c[1] + ("(PK)" if c[5] else "") for c in cols]
            lines.append(f"  {tbl}: {', '.join(col_parts)}")
        lines.append("======================")
        return "\n".join(lines)


@mcp.resource("weather://tables", mime_type="text/plain")
def tables_resource() -> str:
    """Currently loaded tables with row counts."""
    with _conn() as conn:
        tables = _user_tables(conn)
        if not tables:
            return "No tables loaded."
        lines = []
        for tbl in tables:
            count = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            lines.append(f"  {tbl}: {count} rows")
        return "\n".join(lines)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_sources() -> dict:
    """List supported public data sources available for loading."""
    return {"sources": ["open_meteo_weather", "local_csv"]}


@mcp.tool()
def get_loaded_tables() -> dict:
    """
    Return all tables currently in the database with their column names
    and row counts. Use to confirm what data has been loaded.
    """
    with _conn() as conn:
        result = []
        for t in _user_tables(conn):
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
            count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            result.append({"name": t, "columns": cols, "row_count": count})
    return {"tables": result}


# ── run_sql: read-only query execution ───────────────────────────────────────

_ROW_LIMIT = 1000
_FORBIDDEN_KEYWORD = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
    r"PRAGMA|VACUUM|REINDEX|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _strip_sql_comments(query: str) -> str:
    """Remove /* ... */ block comments and -- line comments outside string literals."""
    out = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                out.append(query[i])
                if query[i] == quote:
                    i += 1
                    break
                i += 1
        elif c == "-" and i + 1 < n and query[i + 1] == "-":
            while i < n and query[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and query[i + 1] == "*":
            i += 2
            while i + 1 < n and not (query[i] == "*" and query[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _validate(query: str) -> None:
    """Layer-1 validation: single SELECT/WITH only. Raises ValueError on rejection."""
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    cleaned = _strip_sql_comments(query).strip().rstrip(";")
    statements = [s for s in cleaned.split(";") if s.strip()]
    if len(statements) != 1:
        raise ValueError("only a single SELECT/WITH statement is allowed")
    first_word = statements[0].lstrip().split(None, 1)[0].upper()
    if first_word not in ("SELECT", "WITH"):
        raise ValueError("only read-only queries are allowed")
    if _FORBIDDEN_KEYWORD.search(statements[0]):
        raise ValueError("only read-only queries are allowed")


def _read_only_authorizer(action, arg1, arg2, db_name, trigger_name):
    """SQLite authorizer: deny anything that mutates data or schema."""
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    if action in allowed:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@mcp.tool()
def run_sql(query: str) -> dict:
    """
    Execute a read-only SELECT or WITH query against the cityops database
    and return the result rows.

    The query must be a single SELECT/WITH statement. Writes and DDL
    (INSERT, UPDATE, DELETE, DROP, CREATE, ATTACH, PRAGMA, ...) are rejected
    by layered defenses: a pre-parse keyword check, the PRAGMA query_only
    pragma, and a SQLite authorizer that denies non-read actions at the
    engine level.

    Results are capped at 1000 rows; if more rows would be returned,
    `truncated` is set to true. Use this together with the cityops prompts
    (extreme_value_query, trend_overview_query, ...) which generate the SQL.

    Args:
        query: A single SQL SELECT or WITH statement.

    Returns:
        {
          "columns": [<col-name>, ...],
          "rows":    [[<value>, ...], ...],
          "row_count": <int>,
          "truncated": <bool>
        }
    """
    _validate(query)
    with _conn() as conn:
        conn.execute("PRAGMA query_only = 1")
        conn.set_authorizer(_read_only_authorizer)
        try:
            cursor = conn.execute(query)
        except sqlite3.DatabaseError as e:
            raise ValueError(f"sql error: {e}") from e
        cols = [d[0] for d in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(_ROW_LIMIT + 1)
        truncated = len(fetched) > _ROW_LIMIT
        if truncated:
            fetched = fetched[:_ROW_LIMIT]
        return {
            "columns": cols,
            "rows": [list(r) for r in fetched],
            "row_count": len(fetched),
            "truncated": truncated,
        }


@mcp.tool()
def plan_data_load(query: str) -> dict:
    """
    Analyse a natural-language weather query and return the exact parameters needed
    to satisfy it via load_weather. Returns {"needs_load": false} when no fetch is
    required, or {"needs_load": true, "args": {...}} otherwise.

    Args:
        query: The user's natural-language question.
    """
    q = query.lower()
    has_weather_kw = any(kw in q for kw in _WEATHER_KEYWORDS)
    has_city = any(city in q for city in _SUPPORTED_CITIES)
    has_time_kw = any(kw in q for kw in _TIME_KEYWORDS)
    if not has_weather_kw and not (has_city and has_time_kw):
        return {"needs_load": False}

    today = date.today()
    location = _extract_city(query)
    year_m = _YEAR_RE.search(q)
    explicit_year = int(year_m.group(1)) if year_m else None

    def _r(**kw):
        return {"needs_load": True, "args": {"location": location, **kw}}

    if "summer" in q:
        yr = explicit_year or today.year - 1
        return _r(start_date=f"{yr}-06-21", end_date=f"{yr}-09-22")

    if "winter" in q:
        yr = explicit_year or today.year - 1
        return _r(start_date=f"{yr}-12-21", end_date=f"{yr + 1}-03-20")

    if "spring" in q:
        yr = explicit_year or today.year - 1
        return _r(start_date=f"{yr}-03-21", end_date=f"{yr}-06-20")

    if "fall" in q or "autumn" in q:
        yr = explicit_year or today.year - 1
        return _r(start_date=f"{yr}-09-23", end_date=f"{yr}-12-20")

    if "last year" in q:
        yr = explicit_year or today.year - 1
        return _r(start_date=f"{yr}-01-01", end_date=f"{yr}-12-31")

    if "last week" in q or "past week" in q:
        return _r(start_date=(today - timedelta(days=7)).isoformat(),
                  end_date=(today - timedelta(days=1)).isoformat())

    if "last month" in q:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        last = today.replace(day=1) - timedelta(days=1)
        return _r(start_date=first.isoformat(), end_date=last.isoformat())

    for month_name, month_num in _MONTH_NAMES.items():
        if f"last {month_name}" in q or (month_name in q and "last" in q):
            yr = today.year if month_num < today.month else today.year - 1
            last_day = calendar.monthrange(yr, month_num)[1]
            return _r(start_date=f"{yr}-{month_num:02d}-01",
                      end_date=f"{yr}-{month_num:02d}-{last_day}")

    for month_name, month_num in _MONTH_NAMES.items():
        m = _MONTH_YEAR_RE[month_name].search(q)
        if m:
            yr = int(m.group(1))
            last_day = calendar.monthrange(yr, month_num)[1]
            return _r(start_date=f"{yr}-{month_num:02d}-01",
                      end_date=f"{yr}-{month_num:02d}-{last_day}")

    day_matches = _NAMED_DAY_RE.findall(q)
    if day_matches:
        specific_dates = []
        for month_str, day_str, year_str in day_matches:
            month_num = _MONTH_NAMES[month_str]
            day_num = int(day_str)
            yr = int(year_str) if year_str else (
                today.year if date(today.year, month_num, 1) <= today else today.year - 1
            )
            try:
                specific_dates.append(date(yr, month_num, day_num))
            except ValueError:
                pass
        if specific_dates:
            return _r(start_date=min(specific_dates).isoformat(),
                      end_date=max(specific_dates).isoformat())

    return _r(days=16)


@mcp.tool()
def check_coverage(location: str, start_date: str | None = None,
                   end_date: str | None = None) -> dict:
    """
    Check whether weather_daily already has data for the requested location and range.
    Returns {"covered": true} if sufficient rows exist, {"covered": false} otherwise.

    Args:
        location:   City name (case-insensitive).
        start_date: Start of date range (YYYY-MM-DD). Omit for forecast coverage check.
        end_date:   End of date range (YYYY-MM-DD). Required when start_date is set.
    """
    try:
        loc = location.title()
        with _conn() as conn:
            if start_date and end_date:
                count = conn.execute(
                    "SELECT COUNT(*) FROM weather_daily WHERE location=? AND date>=? AND date<=?",
                    (loc, start_date, end_date),
                ).fetchone()[0]
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM weather_daily WHERE location=? AND date>=date('now')",
                    (loc,),
                ).fetchone()[0]
        return {"covered": count > 0}
    except Exception:
        return {"covered": False}


@mcp.tool()
def load_weather(location: str, days: int = 16,
                 start_date: str | None = None,
                 end_date: str | None = None) -> dict:
    """
    Fetch weather for a city via Open-Meteo and load into weather_daily table.

    Modes:
      - Forecast (default): omit start_date/end_date. Up to 16 days of forecast.
      - Historical range: provide start_date + end_date (YYYY-MM-DD).
        Past dates use observed archive data; future dates within 16 days use forecast.

    Supported cities: atlanta, new york, los angeles, chicago, houston, seattle,
    miami, denver.
    Columns: location, date, temp_max, temp_min, precip_mm, wind_mph, loaded_at.

    Args:
        location:   City name (case-insensitive).
        days:       Forecast days to fetch when not using a date range (max 16).
        start_date: Start of date range (YYYY-MM-DD). Enables historical mode.
        end_date:   End of date range (YYYY-MM-DD). Required when start_date is set.
    """
    try:
        rows = fetch_weather(location, days=days,
                             start_date=start_date, end_date=end_date)
    except Exception as e:
        return {"error": str(e)}

    with _conn() as conn:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='weather_daily'"
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM weather_daily WHERE location = ?", (location.title(),))
            conn.commit()
        cols = _insert_rows(conn, "weather_daily", rows, "open_meteo",
                            f"location={location},start={start_date},end={end_date},days={days}")

    date_range = f"{rows[0]['date']} to {rows[-1]['date']}" if rows else "n/a"
    return {"table_name": "weather_daily", "row_count": len(rows),
            "columns": cols, "location": location.title(), "date_range": date_range}


@mcp.tool()
def load_csv(filename: str) -> dict:
    """
    Load a local CSV file from the configured data directory into the database
    as a new table. Table name is derived from the filename (lowercase, underscores).

    The data directory defaults to a per-user location but can be overridden via
    the CITYOPS_DATA_DIR environment variable.

    Args:
        filename: CSV filename (e.g. 'sales_data.csv').
    """
    csv_dir = get_csv_dir()
    path = csv_dir / filename
    table_name = Path(filename).stem.lower().replace("-", "_").replace(" ", "_")

    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            rows = [dict(r) for r in reader]
    except FileNotFoundError:
        files = [f.name for f in csv_dir.glob("*.csv")]
        return {"error": f"'{filename}' not found in {csv_dir}. Available: {files}"}

    if not rows:
        return {"error": f"'{filename}' is empty"}

    with _conn() as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        conn.commit()
        cols = _insert_rows(conn, table_name, rows, "local_csv", f"file={filename}")

    return {"table_name": table_name, "row_count": len(rows), "columns": cols}


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
def extreme_value_query(location: str, columns: str) -> str:
    """SQL scaffold for finding a single extreme-value record (hottest/coldest/windiest/wettest day)."""
    return (
        f"Find the single extreme-value record for {location}.\n"
        f"Available columns: {columns}\n"
        f"Pattern: SELECT location, date, <relevant_col> FROM weather_daily\n"
        f"  WHERE location = '{location}'\n"
        f"  ORDER BY CAST(<relevant_col> AS REAL) DESC LIMIT 1\n"
        f"Choose DESC for hottest/windiest/wettest, ASC for coldest.\n"
        f"Pass the final SQL to the run_sql tool to execute it and read the rows."
    )


@mcp.prompt()
def trend_overview_query(location: str, columns: str) -> str:
    """SQL scaffold for summarising a period (overview/forecast/what was it like)."""
    return (
        f"Summarise weather for a period in {location}.\n"
        f"Available columns: {columns}\n"
        f"ALWAYS add a date filter that matches the question's time window:\n"
        f"  'this week' / 'this forecast':  AND date BETWEEN date('now') AND date('now', '+6 days')\n"
        f"  'next week':                    AND date BETWEEN date('now', '+7 days') AND date('now', '+13 days')\n"
        f"  named past period (e.g. 'last summer', 'last month'): AND date BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'\n"
        f"  no period specified:            omit the date filter\n"
        f"Pattern: SELECT\n"
        f"  ROUND(AVG(CAST(temp_max AS REAL)),1) AS avg_high_f,\n"
        f"  ROUND(AVG(CAST(temp_min AS REAL)),1) AS avg_low_f,\n"
        f"  ROUND(MAX(CAST(temp_max AS REAL)),1) AS hottest_f,\n"
        f"  ROUND(MIN(CAST(temp_min AS REAL)),1) AS coldest_f,\n"
        f"  SUM(CASE WHEN CAST(precip_mm AS REAL) > 0 THEN 1 ELSE 0 END) AS rainy_days,\n"
        f"  COUNT(*) AS total_days\n"
        f"FROM weather_daily WHERE location = '{location}'\n"
        f"  [AND date BETWEEN ... AND ...   -- always filter to the relevant window]\n"
        f"No LIMIT — aggregate the filtered rows.\n"
        f"Pass the final SQL to the run_sql tool to execute it and read the rows."
    )


@mcp.prompt()
def specific_date_query(location: str, columns: str) -> str:
    """SQL scaffold for a point-in-time lookup (today/tomorrow/yesterday/named date)."""
    return (
        f"Look up weather on a specific date for {location}.\n"
        f"Available columns: {columns}\n"
        f"Pattern: SELECT location, date, temp_max, temp_min, precip_mm, wind_mph\n"
        f"  FROM weather_daily\n"
        f"  WHERE location = '{location}'\n"
        f"    AND date = date('now')            -- today\n"
        f"    AND date = date('now', '+1 day')  -- tomorrow\n"
        f"    AND date = 'YYYY-MM-DD'           -- named date\n"
        f"Use exactly one date filter. No LIMIT needed.\n"
        f"Pass the final SQL to the run_sql tool to execute it and read the rows."
    )


@mcp.prompt()
def comparison_query(location: str, columns: str) -> str:
    """SQL scaffold for comparing multiple cities side by side."""
    return (
        f"Compare weather across cities (starting from {location}).\n"
        f"Available columns: {columns}\n"
        f"Pattern: SELECT location,\n"
        f"  ROUND(AVG(CAST(temp_max AS REAL)),1) AS avg_high_f,\n"
        f"  ROUND(MAX(CAST(temp_max AS REAL)),1) AS hottest_f,\n"
        f"  SUM(CASE WHEN CAST(precip_mm AS REAL) > 0 THEN 1 ELSE 0 END) AS rainy_days\n"
        f"FROM weather_daily\n"
        f"GROUP BY location\n"
        f"ORDER BY avg_high_f DESC\n"
        f"No WHERE location filter — GROUP BY replaces it when comparing cities.\n"
        f"Pass the final SQL to the run_sql tool to execute it and read the rows."
    )


@mcp.prompt()
def aggregation_query(location: str, columns: str) -> str:
    """SQL scaffold for aggregate questions (average/total/how many days/count)."""
    return (
        f"Compute an aggregate statistic for {location}.\n"
        f"Available columns: {columns}\n"
        f"Pattern: SELECT\n"
        f"  COUNT(*) AS total_days,\n"
        f"  SUM(CASE WHEN CAST(precip_mm AS REAL) > 0 THEN 1 ELSE 0 END) AS rainy_days,\n"
        f"  ROUND(AVG(CAST(temp_max AS REAL)),1) AS avg_high_f\n"
        f"FROM weather_daily\n"
        f"WHERE location = '{location}'\n"
        f"Adjust the SELECT columns and aggregation function to match the specific question.\n"
        f"Pass the final SQL to the run_sql tool to execute it and read the rows."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Start the cityops MCP server over stdio."""
    _logger.info("cityops-mcp starting (db=%s)", get_db_path())
    mcp.run()
