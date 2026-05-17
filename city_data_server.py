#!/usr/bin/env python3
"""
Weather MCP server.

Exposes weather data tools (Open-Meteo forecast + historical archive) and local CSVs.
Writes directly to cityops.sqlite so rows are never serialized over stdio.
Returns only lightweight metadata to the MCP client.

Run in dev mode:  fastmcp dev city_data_server.py
"""

import calendar
import csv
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastmcp import FastMCP
from data_sources import fetch_weather

PROJECT_DIR = Path(__file__).parent
DB_PATH     = PROJECT_DIR / "cityops.sqlite"
DATA_DIR    = PROJECT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

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
# Longest names first so "new york" matches before "new" would
_SUPPORTED_CITIES = [
    "new york", "los angeles", "chicago", "houston", "seattle", "miami", "denver", "atlanta",
]


def _extract_city(query: str) -> str:
    q = query.lower()
    for city in _SUPPORTED_CITIES:
        if city in q:
            return city.title()
    return "Atlanta"


mcp = FastMCP("Weather Data Server")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(str(DB_PATH))


def _ensure_source_loads(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_loads (
            source_name  TEXT,
            table_name   TEXT,
            loaded_at    TEXT,
            row_count    INTEGER,
            query_params TEXT
        )
    """)


def _insert_rows(conn, table_name: str, rows: list[dict], source: str,
                 params: str = "") -> list[str]:
    """Create table + insert rows + log to source_loads. Returns column list."""
    if not rows:
        return []
    columns = list(rows[0].keys())
    col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')
    ph = ", ".join("?" for _ in columns)
    for row in rows:
        conn.execute(
            f'INSERT INTO "{table_name}" VALUES ({ph})',
            [str(row.get(c, "")) for c in columns]
        )
    _ensure_source_loads(conn)
    conn.execute(
        "INSERT INTO source_loads VALUES (?, ?, ?, ?, ?)",
        (source, table_name, date.today().isoformat(), len(rows), params)
    )
    conn.commit()
    return columns


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_sources() -> dict:
    """List supported public data sources available for loading."""
    return {"sources": ["open_meteo_weather", "local_csv"]}


@mcp.tool()
def get_loaded_tables() -> dict:
    """
    Return all tables currently in cityops.sqlite with their column names
    and row counts. Use this to confirm what data has been loaded.
    """
    with _conn() as conn:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('source_loads')"
            ).fetchall()
        ]
        result = []
        for t in tables:
            cols  = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
            count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            result.append({"name": t, "columns": cols, "row_count": count})
    return {"tables": result}


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
    has_city       = any(city in q for city in _SUPPORTED_CITIES)
    has_time_kw    = any(kw in q for kw in _TIME_KEYWORDS)
    if not has_weather_kw and not (has_city and has_time_kw):
        return {"needs_load": False}

    today         = date.today()
    location      = _extract_city(query)
    year_m        = re.search(r'\b(20\d\d)\b', q)
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
        last  = today.replace(day=1) - timedelta(days=1)
        return _r(start_date=first.isoformat(), end_date=last.isoformat())

    for month_name, month_num in _MONTH_NAMES.items():
        if f"last {month_name}" in q or (month_name in q and "last" in q):
            yr       = today.year if month_num < today.month else today.year - 1
            last_day = calendar.monthrange(yr, month_num)[1]
            return _r(start_date=f"{yr}-{month_num:02d}-01",
                      end_date=f"{yr}-{month_num:02d}-{last_day}")

    for month_name, month_num in _MONTH_NAMES.items():
        m = re.search(rf"{month_name}\s+(\d{{4}})", q)
        if m:
            yr       = int(m.group(1))
            last_day = calendar.monthrange(yr, month_num)[1]
            return _r(start_date=f"{yr}-{month_num:02d}-01",
                      end_date=f"{yr}-{month_num:02d}-{last_day}")

    day_matches = re.findall(
        r"(january|february|march|april|may|june|july|august|september|october|november|december)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:of\s+)?(\d{4}))?",
        q,
    )
    if day_matches:
        specific_dates = []
        for month_str, day_str, year_str in day_matches:
            month_num = _MONTH_NAMES[month_str]
            day_num   = int(day_str)
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
def check_coverage(location: str, start_date: str = None, end_date: str = None) -> dict:
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
                 start_date: str = None, end_date: str = None) -> dict:
    """
    Fetch weather for a city via Open-Meteo and load into weather_daily table.
    Supports both forecast and historical data.

    Modes:
      - Forecast (default): omit start_date/end_date. Returns up to 16 days of forecast.
      - Historical range: provide start_date + end_date (YYYY-MM-DD).
        Past dates use observed archive data; future dates within 16 days use forecast.

    Supported cities: atlanta, new york, los angeles, chicago, houston, seattle,
    miami, denver.
    Columns: location, date, temp_max, temp_min, precip_mm, wind_mph, loaded_at.

    Args:
        location:   City name (case-insensitive).
        days:       Forecast days to fetch when not using a date range (max 16, default 16).
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
    Load a local CSV file from the /data/ folder into cityops.sqlite as a new table.
    The table name is derived from the filename (lowercase, underscores).

    Args:
        filename: CSV filename (e.g. 'sales_data.csv').
    """
    path = DATA_DIR / filename
    if not path.exists():
        files = [f.name for f in DATA_DIR.glob("*.csv")]
        return {"error": f"'{filename}' not found in /data/. Available: {files}"}

    table_name = Path(filename).stem.lower().replace("-", "_").replace(" ", "_")

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows   = [dict(r) for r in reader]
        cols_from_file = list(reader.fieldnames or [])

    if not rows:
        return {"error": f"'{filename}' is empty"}

    with _conn() as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        conn.commit()
        cols = _insert_rows(conn, table_name, rows, "local_csv", f"file={filename}")

    return {"table_name": table_name, "row_count": len(rows), "columns": cols}


if __name__ == "__main__":
    mcp.run()
