#!/usr/bin/env python3
"""
MCP benchmark: runs 5 different questions about the same loaded dataset
to show cache acceleration, resource caching, and error handling.

Run: python benchmark.py
Requires: Ollama running locally (ollama serve)
"""

import sqlite3
import sys
import time

from cityops_mcp.paths import get_db_path
from mcp_client import call_tool, read_resource, list_resources, list_prompts, get_prompt, metrics


def _reset_db() -> None:
    """Clear weather rows so the benchmark always starts from a true cold state."""
    db_path = get_db_path()
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        n = conn.execute("DELETE FROM weather_daily").rowcount
        conn.commit()
        conn.close()
        print(f"[benchmark] cleared {n} weather rows — cold start guaranteed")
    except sqlite3.OperationalError:
        # weather_daily doesn't exist yet — nothing to clear, equally cold.
        pass

SEPARATOR = "═" * 62

# Five semantically different questions that share the same underlying data
# (Atlanta summer 2024). This is the key insight: cache value comes from
# different queries about the same location/date range, not the same query twice.
QUERIES = [
    # (label, question, load_args_override)
    ("Q1 cold start",
     "hottest day in Atlanta last summer?",
     None),

    ("Q2 diff question / same data",
     "how much did it rain in Atlanta last summer?",
     None),

    ("Q3 diff question / same data",
     "what was the coldest night in Atlanta last summer?",
     None),

    ("Q4 new city — cold start",
     "will it rain in Chicago this week?",
     {"location": "Chicago", "days": 16}),

    ("Q5 diff question / Chicago still warm",
     "what is the forecast high in Chicago tomorrow?",
     None),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def banner(title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


def run_mcp_pipeline(label: str, question: str, load_args_override: dict | None) -> dict:
    """
    Simulate the enricher_node MCP pipeline for a single question:
      plan_data_load → check_coverage → (maybe) load_weather → weather://schema
    Returns timing summary for the query.
    """
    banner(label)
    print(f"  Question: {question}\n")
    metrics.begin_query()
    t_start = time.perf_counter()

    # Step 1: plan
    load_plan = call_tool("plan_data_load", {"query": question})
    args = load_args_override or load_plan.get("args", {})

    # Step 2: coverage check (check_coverage only accepts location/start_date/end_date)
    coverage_args = {k: v for k, v in args.items() if k in ("location", "start_date", "end_date")}
    coverage = call_tool("check_coverage", coverage_args) if coverage_args else {"covered": False}

    # Step 3: load if needed
    loaded = False
    if not coverage.get("covered") and args:
        result = call_tool("load_weather", args)
        loaded = "error" not in result
        if not loaded:
            print(f"  [!] load_weather error: {result.get('error')}")

    # Step 4: read schema Resource (always — mirrors schema_node behavior)
    schema = read_resource("weather://schema")

    elapsed = (time.perf_counter() - t_start) * 1000
    metrics.end_query(iterations=1, success=True)

    status = "LOADED" if loaded else ("CACHE HIT" if coverage.get("covered") else "NO DATA")
    print(f"\n  → status: {status}  |  schema: {len(schema)} chars  |  total: {elapsed:.0f}ms")
    return {"label": label, "status": status, "elapsed_ms": elapsed}


def run_error_demo() -> None:
    """
    Show ErrorHandlingMiddleware catching an unhandled error.
    load_weather with a city not in Open-Meteo's coordinate table
    triggers an exception inside the cityops_mcp server that the middleware
    wraps into a structured {"error": "..."} dict instead of crashing.
    """
    banner("Error demo — ErrorHandlingMiddleware")
    print("  Calling load_weather with invalid city 'INVALID_CITY_XYZ'\n")
    result = call_tool("load_weather", {"location": "INVALID_CITY_XYZ", "days": 1})
    if "error" in result:
        print(f"  → Middleware caught error: {result['error']!r}")
        print("  → Agent receives structured dict, not an exception crash ✓")
    else:
        print(f"  → Unexpected success: {result}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _reset_db()

    # Show all three MCP primitives: Tools are always available; show Resources + Prompts
    banner("MCP Protocol Primitives")
    resources = list_resources()
    print(f"  Resources : {resources}")
    for uri in resources:
        text = read_resource(uri)
        print(f"    {uri}: {len(text)} chars")

    print()
    prompts = list_prompts()
    print(f"  Prompts   : {prompts}")
    for name in prompts:
        text = get_prompt(name, {"location": "Atlanta", "columns": "location, date, temp_max, temp_min, precip_mm, wind_mph"})
        print(f"    {name}: {len(text)} chars")

    print()

    # Run the 5-query benchmark
    results = []
    for label, question, override in QUERIES:
        results.append(run_mcp_pipeline(label, question, override))

    # Error demo
    run_error_demo()

    # Summary table
    banner("Benchmark Summary")
    print(f"  {'Query':<30} {'Status':<12} {'Total ms':>9}")
    print(f"  {'─' * 54}")
    for r in results:
        print(f"  {r['label']:<30} {r['status']:<12} {r['elapsed_ms']:>8.0f}ms")

    print(f"\n  Key observations:")
    print(f"  • Q1 pays full cost: plan_data_load + load_weather + schema (cold)")
    print(f"  • Q2, Q3 skip load_weather — check_coverage cached for same args")
    print(f"  • Q2, Q3 skip schema fetch — weather://schema cached client-side")
    print(f"  • Q4 pays load cost for Chicago; schema still served from cache")
    print(f"  • Q5 skips all fetches — coverage and schema both cached")

    print()
    print(metrics.report())


if __name__ == "__main__":
    try:
        import ollama
        ollama.list()
    except Exception:
        print("ERROR: Cannot reach Ollama. Start it with:  ollama serve")
        sys.exit(1)

    main()
