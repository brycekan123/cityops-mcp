import json
from typing import TypedDict, List, Optional

from langgraph.graph import StateGraph, END

from database import (
    list_tables, get_schema, get_col_names, sample_rows, query_database,
    check_date_range, find_join_path,
)
from llm_helper import llm, extract_sql, MODEL
from mcp_client import call_tool as mcp_call, read_resource, get_prompt, list_prompts

MAX_ITER       = 5
CONF_THRESHOLD = 0.85

# ── Display helpers ───────────────────────────────────────────────────────────
W = 62

def _hdr(label: str) -> None:
    line = f"{'━' * W}"
    print(f"\n{line}")
    print(f" {label}  ›  LLM [{MODEL}]")
    print(line)

def _show_sql(sql: str) -> None:
    print("  SQL:")
    for ln in sql.strip().splitlines():
        print(f"    {ln}")

def _show_verdict(verdict: dict, passed: bool, itr: int, max_itr: int) -> None:
    confidence = float(verdict.get("confidence", 0))
    err_class  = verdict.get("error_class", "")
    hint       = verdict.get("strategy_hint", "")

    if passed:
        print(f"  ✓ PASS  confidence={confidence:.2f}  →  final answer")
    else:
        next_attempt = itr + 1
        exhausted    = next_attempt >= max_itr
        routing      = "final answer (max retries)" if exhausted else f"retry {next_attempt + 1}/{max_itr}"
        print(f"  ✗ FAIL  [{err_class}]  confidence={confidence:.2f}  →  {routing}")
        if hint and hint != "none":
            print(f"  Hint : {hint}")


# ── State ─────────────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    original_query  : str
    plan            : Optional[dict]
    schema_snapshot : Optional[str]
    prompt_scaffold : Optional[str]
    loaded_location : Optional[str]   # city confirmed loaded by enricher
    sql_attempts    : List[str]
    query_results   : List[dict]
    judge_verdicts  : List[dict]
    iteration       : int
    final_answer    : Optional[str]
    status          : str


# ── Planner ───────────────────────────────────────────────────────────────────
PLANNER_SYS = """You are a query planner for a multi-city weather agent.
Respond ONLY with valid JSON — no prose, no markdown.

JSON schema:
{
  "intent":        "what the user wants",
  "analysis_goal": "one sentence",
  "tables":        ["weather_daily"],
  "strategy":      "one-sentence SQL approach"
}

Always use tables: ["weather_daily"] only — never join with other tables.
Default location is "Atlanta" unless the user specifies otherwise."""




def planner_node(state: AgentState) -> AgentState:
    _hdr("PLANNER")
    tables = list_tables()
    response = llm(
        PLANNER_SYS,
        f"Query: {state['original_query']}\nAvailable tables: {tables}",
        json_mode=True,
    )
    plan = json.loads(response)
    print(f"  Tables   : {', '.join(plan.get('tables', []))}")
    print(f"  Strategy : {plan.get('strategy', '')}")
    return {**state, "plan": plan, "status": "running"}


# ── Enricher ──────────────────────────────────────────────────────────────────

def enricher_node(state: AgentState) -> AgentState:
    print(f"\n{'━' * W}")
    print(f" ENRICHER  ›  MCP data loader")
    print(f"{'━' * W}")

    # Judge may have injected explicit tool_calls for a forced reload
    explicit_calls = [tc for tc in state["plan"].get("tool_calls", []) if tc]

    if explicit_calls:
        tc     = explicit_calls[0]
        result = mcp_call(tc["tool_name"], tc["args"])
        location = result.get("location") or tc["args"].get("location")
    else:
        load_plan = mcp_call("plan_data_load", {"query": state["original_query"]})
        if not load_plan.get("needs_load"):
            print("[ENRICHER] no data load needed")
            return state
        load_args     = load_plan["args"]
        coverage_args = {k: v for k, v in load_args.items()
                         if k in ("location", "start_date", "end_date")}
        coverage = mcp_call("check_coverage", coverage_args)
        if coverage.get("covered"):
            print("[ENRICHER] data already in DB — skipping")
            return {**state, "loaded_location": load_args.get("location")}
        result   = mcp_call("load_weather", load_args)
        location = result.get("location") or load_args.get("location")

    if "error" in result:
        print(f"[ENRICHER] MCP error: {result['error']}")
        return state

    if "table_name" in result:
        table   = result["table_name"]
        updated = {**state["plan"], "tables": list(set(state["plan"].get("tables", []) + [table]))}
        print(f"[ENRICHER] table '{table}' ({result['row_count']} rows) ready")
        return {**state, "plan": updated, "loaded_location": location}

    print(f"[ENRICHER] {result}")
    return state


# ── Schema + Prompt ───────────────────────────────────────────────────────────

# Maps query intent to an MCP Prompt name (server-managed SQL templates).
# This replaces the hardcoded _EXTREME_TRIGGERS / _SPECIFIC_TRIGGERS / _OVERVIEW_TRIGGERS.
def _classify_intent(query: str) -> str:
    q = query.lower()
    if any(t in q for t in {"hottest", "coldest", "warmest", "coolest", "highest",
                              "lowest", "maximum", "minimum", "max", "min", "windiest", "wettest"}):
        return "extreme_value_query"
    # Point-in-time takes priority — "forecast high tomorrow" is specific_date, not trend_overview
    if any(t in q for t in {"tomorrow", "today", "yesterday", "will it", "on "}):
        return "specific_date_query"
    # Multi-day windows and general forecasts
    if any(t in q for t in {"this week", "next week", "this month", "week", "forecast"}):
        return "trend_overview_query"
    if any(t in q for t in {"compare", "which city", "cities", "vs", "versus"}):
        return "comparison_query"
    if any(t in q for t in {"average", "avg", "total", "count", "how many days"}):
        return "aggregation_query"
    return "trend_overview_query"


def schema_node(state: AgentState) -> AgentState:
    """Fetch schema Resource + matching MCP Prompt scaffold after any enrichment."""
    import re as _re
    schema = read_resource("weather://schema")
    tables = list_tables()["tables"]

    # Parse live column names from the schema snapshot
    col_match = _re.search(r"weather_daily:\s*(.+)", schema)
    columns   = col_match.group(1).strip() if col_match else "location, date, temp_max, temp_min, precip_mm, wind_mph"

    # Classify intent and fetch the matching server-managed SQL scaffold
    intent   = _classify_intent(state["original_query"])
    scaffold = get_prompt(intent, {"location": "the city", "columns": columns})

    print(f"\n{'━' * W}")
    print(f" SCHEMA  ›  MCP Resource  +  Prompt [{intent}]")
    print(f"{'━' * W}")
    print(f"[SCHEMA] tables: {tables}")

    return {**state, "schema_snapshot": schema, "prompt_scaffold": scaffold}


# ── Actor ─────────────────────────────────────────────────────────────────────

ACTOR_SYS = """You are a senior SQL engineer. Write one correct SQLite query.
Rules:
- Output ONLY raw SQL — no explanation, no markdown, no commentary
- ONLY join tables that share an explicit foreign key in the schema — never invent joins
- weather_daily columns: location (TEXT), date (TEXT), temp_max (REAL), temp_min (REAL), precip_mm (REAL), wind_mph (REAL)
- Query weather directly: WHERE location = '<city>' — no join with other tables needed
- The loaded location is visible in the sample rows above — use that exact string in WHERE location = '...'
- CRITICAL — DATE FILTERS ON RANGE QUERIES:
  For historical period queries ("last summer", "August 2024", "last month"):
    weather_daily is pre-loaded for exactly that period.
    Use ONLY WHERE location = '<loc>' — DO NOT add date filters, the data is already scoped.
  For explicit time-window queries ("this week", "next week", "this month"):
    A DATE FILTER REQUIRED line will appear above — you MUST copy that exact filter into your SQL.

  CORRECT (historical):   SELECT location, date, temp_max FROM weather_daily WHERE location = '<loc>' ORDER BY CAST(temp_max AS REAL) DESC LIMIT 1
  CORRECT (this week):    SELECT ... FROM weather_daily WHERE location = '<loc>' AND date BETWEEN date('now') AND date('now', '+6 days')
  WRONG:                  SELECT ... WHERE location = '<loc>' AND date >= '2025-06-01' AND date <= '2025-09-22' ...

  For specific-point queries only (today/tomorrow/yesterday/a named date), add an exact date:
    tomorrow → WHERE location = '<loc>' AND date = date('now', '+1 day')
    today    → WHERE location = '<loc>' AND date = date('now')
    named    → WHERE location = '<loc>' AND date = '2026-05-04'
- ALWAYS include date in the SELECT for single-row results — the user needs to know WHEN
- Extreme value (hottest/coldest/windiest day): SELECT location, date, temp_max, temp_min FROM weather_daily WHERE location = '<loc>' ORDER BY CAST(temp_max AS REAL) DESC LIMIT 1
- Overview/summary ("what was it like", "how was", "tell me about", "describe"): aggregate across all rows:
    SELECT ROUND(AVG(CAST(temp_max AS REAL)),1) AS avg_high_f, ROUND(AVG(CAST(temp_min AS REAL)),1) AS avg_low_f, ROUND(MAX(CAST(temp_max AS REAL)),1) AS hottest_f, ROUND(MIN(CAST(temp_min AS REAL)),1) AS coldest_f, SUM(CASE WHEN CAST(precip_mm AS REAL) > 0 THEN 1 ELSE 0 END) AS rainy_days, COUNT(*) AS total_days FROM weather_daily WHERE location = '<loc>'
- Rainy days list: SELECT date, precip_mm FROM weather_daily WHERE location = '<loc>' AND CAST(precip_mm AS REAL) > 0 ORDER BY date
- Yes/no rain (will it rain tomorrow, did it rain today): SELECT date, precip_mm FROM weather_daily WHERE location = '<loc>' AND date = date('now', '+1 day') — no precip filter, return the row as-is
- SQLite date arithmetic if needed: date('now', '-7 days')  NOT INTERVAL syntax
- Always use explicit table aliases"""


def actor_node(state: AgentState) -> AgentState:
    itr    = state["iteration"]
    all_tables = set(list_tables().get("tables", []))
    tables = [t for t in state["plan"].get("tables", []) if t in all_tables]
    _hdr(f"ACTOR  attempt {itr + 1}/{MAX_ITER}")

    ctx = []
    # Location is set by enricher_node from the actual load result — never inferred from sample rows.
    # This prevents the actor from defaulting to whichever city happens to appear first in the DB.
    loaded_location = state.get("loaded_location")
    for tbl in tables:
        schema = get_schema(tbl)
        sample = sample_rows(tbl)
        # Filter sample rows to the confirmed city so the actor sees the right location examples
        if tbl == "weather_daily" and loaded_location and sample.get("rows"):
            city_rows = [r for r in sample["rows"] if r.get("location") == loaded_location]
            if city_rows:
                sample = {**sample, "rows": city_rows}
        ctx.append(f"--- {tbl} schema ---\n{json.dumps(schema, indent=2)}")
        ctx.append(f"--- {tbl} sample ---\n{json.dumps(sample, indent=2)}")
        if tbl == "weather_daily":
            dr = check_date_range("weather_daily", "date")
            ctx.append(
                f"--- weather_daily loaded range ---\n"
                f"min_date={dr.get('min')}  max_date={dr.get('max')}  rows={dr.get('row_count')}"
            )
    print(f"  Context  : get_schema + sample_rows for {', '.join(tables) or '(none — using schema snapshot)'}")

    retry_block = ""
    if state["sql_attempts"]:
        retry_block = "\n\n=== PREVIOUS FAILED ATTEMPTS ===\n"
        for i, (sql, verdict) in enumerate(
                zip(state["sql_attempts"], state["judge_verdicts"]), 1):
            retry_block += (
                f"Attempt {i}:\n"
                f"  SQL:           {sql}\n"
                f"  error_class:   {verdict.get('error_class')}\n"
                f"  error_msg:     {verdict.get('error_msg')}\n"
                f"  strategy_hint: {verdict.get('strategy_hint')}\n"
            )

        last  = state["judge_verdicts"][-1]
        tool  = last.get("tool_to_call", "none")
        tbl   = last.get("tool_table")
        tbl_b = last.get("tool_table_b")
        dcol  = last.get("tool_date_col")
        tool_result = None

        if tool == "get_col_names" and tbl:
            tool_result = get_col_names(tbl)
            print(f"  Debug    : get_col_names({tbl!r}) → {tool_result['columns']}")
        elif tool == "check_date_range" and tbl and dcol:
            tool_result = check_date_range(tbl, dcol)
            print(f"  Debug    : check_date_range({tbl!r}, {dcol!r}) → min={tool_result.get('min')} max={tool_result.get('max')} rows={tool_result.get('row_count')}")
        elif tool == "find_join_path" and tbl and tbl_b:
            tool_result = find_join_path(tbl, tbl_b)
            print(f"  Debug    : find_join_path({tbl!r}, {tbl_b!r}) → direct={tool_result['direct_join_cols']} indirect={[p['via'] for p in tool_result['indirect_paths']]}")

        if tool_result:
            retry_block += f"\n=== DEBUG TOOL: {tool} ===\n{json.dumps(tool_result, indent=2)}\n"

    schema_ref = state.get("schema_snapshot") or ""
    scaffold   = state.get("prompt_scaffold") or ""

    # Strip strategy from what the actor sees — it contains the planner's SQL guess
    # which has wrong date ranges and column names that bleed into the generated SQL.
    actor_plan = {k: v for k, v in state["plan"].items()
                  if k not in ("strategy", "caveats")}
    actor_plan["tables"] = tables

    # Pin the location at the top of the prompt so the LLM can't confuse it with another city.
    loc_line = (
        f"LOADED LOCATION: '{loaded_location}' — you MUST use WHERE location = '{loaded_location}' in your SQL.\n\n"
        if loaded_location else ""
    )

    # For explicit time-window queries, pin the exact SQLite date expression so the model
    # can't omit the filter. Small models ignore vague retry hints like "add a date filter".
    _q = state["original_query"].lower()
    if "next week" in _q:
        date_hint_line = "DATE FILTER REQUIRED — include in your WHERE clause: AND date BETWEEN date('now', '+7 days') AND date('now', '+13 days')\n\n"
    elif any(t in _q for t in ("this week", "this forecast", "forecast this week")):
        date_hint_line = "DATE FILTER REQUIRED — include in your WHERE clause: AND date BETWEEN date('now') AND date('now', '+6 days')\n\n"
    elif "this month" in _q:
        date_hint_line = "DATE FILTER REQUIRED — include in your WHERE clause: AND date BETWEEN date('now', 'start of month') AND date('now', 'start of month', '+1 month', '-1 day')\n\n"
    else:
        date_hint_line = ""

    # Inject the MCP Prompt scaffold (server-managed SQL template for this intent type)
    scaffold_block = f"\n=== SQL PATTERN (from MCP Prompt) ===\n{scaffold}\n======================================\n" if scaffold else ""

    user_prompt = (
        loc_line
        + date_hint_line
        + schema_ref + "\n\n"
        + scaffold_block
        + f"Original question: {state['original_query']}\n"
        + f"Plan:\n{json.dumps(actor_plan, indent=2)}\n\n"
        + f"Table details (schema + sample rows):\n" + "\n".join(ctx) + retry_block
        + "\n\nWrite the SQL query now:"
    )
    raw = llm(ACTOR_SYS, user_prompt)
    sql = extract_sql(raw)
    _show_sql(sql)

    result = query_database(sql)
    err_str = f"  error: {result['error']}" if result["error"] else ""
    print(f"  Result   : {result['row_count']} rows{err_str}")
    if result["rows"]:
        print(f"  Preview  : {result['rows'][:2]}")

    return {
        **state,
        "sql_attempts" : state["sql_attempts"]  + [sql],
        "query_results": state["query_results"] + [result],
    }


# ── Judge ─────────────────────────────────────────────────────────────────────

# Only called when SQL ran successfully and returned rows — purely semantic check.
JUDGE_SYS = """You are a SQL result reviewer. The query ran successfully and returned rows.
Your job: does the RESULT DATA answer the user's question?

Evaluate the result, not the SQL. Ignore how the SQL was written — focus on what the data shows.

PASS if the result columns contain data that addresses the question for the right city.
FAIL (pick one) only for clear-cut semantic errors:
- wrong_aggregation: result uses the wrong aggregate (e.g. one average row when "which day was hottest" needs a single extreme-value row with a date)
- missing_filter: result data is for the WRONG entity (e.g. shows Atlanta rows when the question is about Chicago)
- wrong_table: queried the wrong table entirely

CRITICAL:
- Evaluate the result rows and columns — NOT whether the SQL has a specific WHERE clause
- If the result contains relevant columns (rainy_days, precip_mm, temp_max, dates) that address the question → PASS
- wrong_aggregation only fires when the aggregation type is clearly wrong for the question shape (extreme vs. average)
- missing_filter only fires when the result is for the WRONG scope — not because a WHERE clause is absent
- Unusual values (87°F coldest day, 5 rainy days out of 7) → PASS, trust the data

Respond ONLY with valid JSON — no prose, no markdown.
{
  "status":        "pass" or "fail",
  "error_class":   "none|wrong_aggregation|missing_filter|wrong_table",
  "error_msg":     "specific issue or 'none'",
  "strategy_hint": "one-sentence fix for the actor, or 'none'",
  "confidence":    0.95
}"""


def _programmatic_check(result: dict) -> dict | None:
    """Return a verdict dict for clear-cut failures, or None to let the LLM judge."""
    if result["error"]:
        err = result["error"]
        # Guess which debug tool will help most
        if "no such column" in err:
            tool, tbl = "get_col_names", err.split(":")[-1].strip().split(".")[0]
        else:
            tool, tbl = "none", None
        return {
            "status": "fail", "error_class": "syntax_error",
            "error_msg": err,
            "strategy_hint": f"SQL failed: {err}. Fix and rewrite.",
            "tool_to_call": tool, "tool_table": tbl,
            "tool_table_b": None, "tool_date_col": None,
            "confidence": 0.0,
        }
    if result["row_count"] == 0:
        return {
            "status": "fail", "error_class": "empty_result",
            "error_msg": "Query returned 0 rows — if this is a yes/no question the filter may be wrong (e.g. filtering precip_mm > 0 for 'will it rain').",
            "strategy_hint": "If asking whether something will happen: SELECT the value without filtering it out. If asking for records: remove or broaden filters.",
            "tool_to_call": "none", "tool_table": None,
            "tool_table_b": None, "tool_date_col": None,
            "confidence": 0.0,
        }
    return None


def judge_node(state: AgentState) -> AgentState:
    itr    = state["iteration"]
    sql    = state["sql_attempts"][-1]
    result = state["query_results"][-1]
    _hdr(f"JUDGE  attempt {itr + 1}")

    # Objective checks — no LLM needed
    verdict = _programmatic_check(result)

    if verdict is None:
        # SQL ran and has rows — ask LLM only about semantic correctness
        user_prompt = (
            f"Question: {state['original_query']}\n\n"
            f"SQL:\n{sql}\n\n"
            f"Result sample ({result['row_count']} rows total):\n"
            f"{json.dumps(result['rows'][:5], indent=2)}\n\n"
            "Does this result correctly and completely answer the question?"
        )
        raw     = llm(JUDGE_SYS, user_prompt, json_mode=True)
        verdict = json.loads(raw)
        # fill tool fields the LLM prompt no longer asks for
        verdict.setdefault("tool_to_call",  "none")
        verdict.setdefault("tool_table",    None)
        verdict.setdefault("tool_table_b",  None)
        verdict.setdefault("tool_date_col", None)

    passed = verdict.get("status") == "pass" and float(verdict.get("confidence", 0)) >= CONF_THRESHOLD

    # ── MCP reload recovery ───────────────────────────────────────────────────
    # If we got 0 rows, check whether the SQL asked for a date range that isn't
    # in the DB. If so, reload via enricher instead of blindly retrying the actor.
    # Only attempt once (itr == 1 means second judge call = first retry).
    new_status = None
    if not passed and result["row_count"] == 0 and itr == 1:
        import re as _re
        dates_in_sql = _re.findall(r"'(\d{4}-\d{2}-\d{2})'", sql)
        if dates_in_sql:
            sd, ed  = min(dates_in_sql), max(dates_in_sql)
            loc_hit = _re.search(r"location\s*=\s*'([^']+)'", sql, _re.IGNORECASE)
            loc     = loc_hit.group(1) if loc_hit else "Atlanta"
            reload_args = {"location": loc, "start_date": sd, "end_date": ed}
            coverage    = mcp_call("check_coverage", reload_args)
            if not coverage.get("covered"):
                print(f"  [reload] DB doesn't cover {sd}→{ed} — re-enriching")
                reload_tc    = {"tool_name": "load_weather", "args": reload_args}
                updated_plan = {**state["plan"], "tool_calls": [reload_tc]}
                return {
                    **state,
                    "plan"          : updated_plan,
                    "judge_verdicts": state["judge_verdicts"] + [verdict],
                    "iteration"     : itr + 1,
                    "status"        : "needs_reload",
                }

    # Short-circuit: same 0-row SQL twice in a row → give up
    stuck = (
        not passed
        and result["row_count"] == 0
        and len(state["sql_attempts"]) >= 2
        and state["sql_attempts"][-1] == state["sql_attempts"][-2]
        and state["query_results"][-2]["row_count"] == 0
    )
    exhausted  = (itr + 1) >= MAX_ITER or stuck
    new_status = new_status or ("pass" if passed else ("failed" if exhausted else "running"))

    _show_verdict(verdict, passed, itr, MAX_ITER)

    return {
        **state,
        "judge_verdicts": state["judge_verdicts"] + [verdict],
        "iteration"     : itr + 1,
        "status"        : new_status,
    }


# ── Final Answer ──────────────────────────────────────────────────────────────
FINAL_SYS = """You are a helpful data analyst. Synthesize query results into a concise,
accurate natural language answer. Lead with the key finding. Be specific with numbers.
CRITICAL: Use ONLY the values in the data rows. Never invent, estimate, or paraphrase dates or
numbers — copy them exactly as they appear. If a date column is missing from the result, say so."""


def final_answer_node(state: AgentState) -> AgentState:
    result = state["query_results"][-1]

    if result["error"]:
        answer = (
            f"Unable to answer after {state['iteration']} attempts. "
            f"Last error: {result['error']}"
        )
    elif result["row_count"] == 0:
        # Don't hallucinate — tell the user honestly what data is available
        date_hint = ""
        try:
            dr = check_date_range("weather_daily", "date")
            if dr.get("min"):
                date_hint = f" Weather data covers {dr['min']} to {dr['max']}."
        except Exception:
            pass
        answer = (
            f"No data matched your query.{date_hint} "
            f"Try asking about a specific city and time range — e.g., "
            f"'hottest day last summer in Chicago?' or 'what was Atlanta like in August 2024?'"
        )
    else:
        _hdr("SYNTHESIZER")
        answer = llm(
            FINAL_SYS,
            f"Question: {state['original_query']}\n\nData:\n{json.dumps(result['rows'], indent=2)}\n\nAnswer:"
        )

    return {**state, "final_answer": answer}


# ── Graph ─────────────────────────────────────────────────────────────────────
def route_after_judge(state: AgentState) -> str:
    s = state["status"]
    if s in ("pass", "failed"):
        return "final_answer"
    if s == "needs_reload":
        return "enricher"
    return "actor"


builder = StateGraph(AgentState)
builder.add_node("planner",      planner_node)
builder.add_node("enricher",     enricher_node)
builder.add_node("schema",       schema_node)
builder.add_node("actor",        actor_node)
builder.add_node("judge",        judge_node)
builder.add_node("final_answer", final_answer_node)
builder.set_entry_point("planner")
builder.add_edge("planner",  "enricher")
builder.add_edge("enricher", "schema")
builder.add_edge("schema",   "actor")
builder.add_edge("actor",    "judge")
builder.add_conditional_edges(
    "judge",
    route_after_judge,
    {"actor": "actor", "enricher": "enricher", "final_answer": "final_answer"},
)
builder.add_edge("final_answer", END)

agent = builder.compile()


# ── Public API ────────────────────────────────────────────────────────────────
def run(query: str) -> dict:
    state = AgentState(
        original_query=query,
        plan=None,
        schema_snapshot=None,
        prompt_scaffold=None,
        loaded_location=None,
        sql_attempts=[],
        query_results=[],
        judge_verdicts=[],
        iteration=0,
        final_answer=None,
        status="running",
    )
    return agent.invoke(state)
