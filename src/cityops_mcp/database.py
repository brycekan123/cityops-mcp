"""SQLite query helpers consumed by the in-repo agent demo."""

from __future__ import annotations

import sqlite3

from cityops_mcp.paths import get_db_path


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(get_db_path()), check_same_thread=False)


def list_tables() -> dict:
    rows = _conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT IN ('source_loads')"
    ).fetchall()
    return {"tables": [r[0] for r in rows]}


def get_schema(table_name: str) -> dict:
    cols = _conn().execute(f"PRAGMA table_info({table_name})").fetchall()
    return {
        "table": table_name,
        "columns": [{"name": c[1], "type": c[2], "pk": bool(c[5])} for c in cols],
    }


def get_col_names(table_name: str) -> dict:
    cols = _conn().execute(f"PRAGMA table_info({table_name})").fetchall()
    return {"table": table_name, "columns": [c[1] for c in cols]}


def sample_rows(table_name: str, n: int = 3) -> dict:
    rows = _conn().execute(f'SELECT * FROM "{table_name}" LIMIT {n}').fetchall()
    names = [c[1] for c in _conn().execute(f"PRAGMA table_info({table_name})").fetchall()]
    return {"table": table_name, "rows": [dict(zip(names, r, strict=False)) for r in rows]}


def check_date_range(table_name: str, date_col: str) -> dict:
    try:
        row = _conn().execute(
            f'SELECT MIN({date_col}), MAX({date_col}), COUNT(*) FROM "{table_name}"'
        ).fetchone()
        return {"table": table_name, "date_col": date_col,
                "min": row[0], "max": row[1], "row_count": row[2]}
    except sqlite3.Error as e:
        return {"table": table_name, "date_col": date_col, "error": str(e)}


def find_join_path(table_a: str, table_b: str) -> dict:
    def cols(t):
        return {c[1]: c[2] for c in _conn().execute(f"PRAGMA table_info({t})").fetchall()}

    all_tables = [r[0] for r in _conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    ca, cb = cols(table_a), cols(table_b)
    direct = [c for c in ca if c in cb]
    indirect = []
    for t in all_tables:
        if t in (table_a, table_b):
            continue
        ct = set(cols(t))
        a_links = [c for c in ca if c in ct]
        b_links = [c for c in cb if c in ct]
        if a_links and b_links:
            indirect.append({"via": t, f"{table_a}_cols": a_links, f"{table_b}_cols": b_links})
    return {"table_a": table_a, "table_b": table_b,
            "direct_join_cols": direct, "indirect_paths": indirect}


def query_database(sql: str) -> dict:
    try:
        cur = _conn().execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return {"rows": [dict(zip(cols, r, strict=False)) for r in rows],
                "row_count": len(rows), "error": None}
    except Exception as e:
        return {"rows": [], "row_count": 0, "error": str(e)}
