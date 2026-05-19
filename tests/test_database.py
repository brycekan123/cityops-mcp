"""Smoke tests for the database helper module."""

from __future__ import annotations

import cityops_mcp.database as db


def test_list_tables_empty_db():
    assert db.list_tables() == {"tables": []}


def test_query_database_returns_error_on_bad_sql():
    result = db.query_database("SELECT * FROM does_not_exist")
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["error"] is not None
    assert "no such table" in result["error"].lower()


def test_query_database_returns_rows_on_valid_sql():
    conn = db._conn()
    conn.execute('CREATE TABLE t (a TEXT, b TEXT)')
    conn.execute("INSERT INTO t VALUES ('1', 'x')")
    conn.execute("INSERT INTO t VALUES ('2', 'y')")
    conn.commit()
    conn.close()

    result = db.query_database("SELECT a, b FROM t ORDER BY a")
    assert result["error"] is None
    assert result["row_count"] == 2
    assert result["rows"] == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]


def test_get_col_names_after_create():
    conn = db._conn()
    conn.execute('CREATE TABLE weather_daily (location TEXT, date TEXT, temp_max REAL)')
    conn.commit()
    conn.close()

    cols = db.get_col_names("weather_daily")
    assert cols == {"table": "weather_daily",
                    "columns": ["location", "date", "temp_max"]}
