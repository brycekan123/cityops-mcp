# Agentic SQL Chatbot

A local weather chatbot powered by a multi-node LangGraph agent, Ollama (local LLM), and an MCP data server. Ask natural-language questions about weather across 8 US cities — the agent fetches live data, writes SQL, self-corrects, and answers in plain English.

```
what's the forecast for Atlanta this week?
hottest day last summer in Chicago?
coldest night in New York last January?
will it rain in Miami tomorrow?
which days had rain last month in Seattle?
```

---

## Running

**Prerequisites**

- Python 3.10+
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- A pulled model, e.g. `ollama pull llama3.1`

**Install and run**

```bash
pip install -r requirements.txt
python chatbot.py
```

That's it. The MCP server, SQLite database, and LangGraph agent all start automatically.

**Supported cities:** Atlanta, Chicago, New York, Los Angeles, Houston, Seattle, Miami, Denver

**Change the model** (optional):
```bash
OLLAMA_MODEL=llama3.2 python chatbot.py
```

---

## Architecture

Five files do all the work:

| File | Role |
|---|---|
| `chatbot.py` | CLI loop — takes input, calls agent, prints answer |
| `agent.py` | LangGraph graph — orchestrates the full pipeline |
| `city_data_server.py` | MCP server — fetches weather and exposes data tools |
| `database.py` | SQLite helpers — schema, sampling, querying |
| `llm_helper.py` | Ollama wrapper — single `llm()` call used everywhere |

---

## Step-by-Step Agentic Workflow

Every query passes through six nodes in a LangGraph state machine. Here's exactly what happens at each step.

```
User query
    │
    ▼
┌─────────┐
│ PLANNER │  LLM decides SQL intent and which tables to use
└────┬────┘
     │
     ▼
┌──────────┐
│ ENRICHER │  MCP fetches weather data if not already in DB
└────┬─────┘
     │
     ▼
┌────────┐
│ SCHEMA │  Refreshes schema snapshot after any new data load
└───┬────┘
    │
    ▼
┌───────┐
│ ACTOR │  LLM writes and runs the SQL query
└───┬───┘
    │
    ▼
┌───────┐        ┌──────────┐
│ JUDGE │──fail──► ENRICHER │  (reload path — see below)
└───┬───┘        └──────────┘
    │ pass
    ▼
┌──────────────┐
│ FINAL ANSWER │  LLM synthesizes data rows into plain English
└──────────────┘
```

---

### 1. Planner

The planner is the first LLM call. It receives the user's query and the list of available tables and outputs a compact JSON plan:

```json
{
  "intent":        "find the hottest day last summer in Chicago",
  "analysis_goal": "return the date with the highest temp_max",
  "tables":        ["weather_daily"],
  "strategy":      "SELECT date, temp_max ORDER BY temp_max DESC LIMIT 1"
}
```

The planner's only job is **SQL semantics** — what the user wants and which tables to query. It does not decide what data to load; that responsibility belongs entirely to the MCP enricher.

---

### 2. Enricher (MCP)

The enricher is where the Model Context Protocol comes in. Before the agent can write SQL, it needs the right data in the database.

**Step 1 — Plan the load**

The enricher calls the MCP tool `plan_data_load(query)` on the local MCP server (`city_data_server.py`). This tool parses the natural-language query with deterministic heuristics to figure out exactly what to fetch:

```
"hottest day last summer in Chicago"
    → needs_load: true
    → location: Chicago
    → start_date: 2024-06-21
    → end_date:   2024-09-22
```

**Step 2 — Check coverage**

The enricher calls `check_coverage(location, start_date, end_date)` — another MCP tool that queries the SQLite database to see if that date range is already loaded. If it is, the load is skipped entirely.

**Step 3 — Fetch data**

If coverage is missing, the enricher calls `load_weather(location, start_date, end_date)`, which hits the [Open-Meteo API](https://open-meteo.com) (free, no key required) and writes the rows directly into the `weather_daily` table in `cityops.sqlite`.

The enricher returns lightweight metadata to the agent — just the table name and row count — never the raw rows.

---

### 3. Schema

After enrichment, a schema node rebuilds a fresh snapshot of the database:

```
=== DATABASE SCHEMA ===
  weather_daily: location, date, temp_max, temp_min, precip_mm, wind_mph
======================
```

This snapshot is injected into the actor's context on every attempt, so the LLM always sees the real column names after a data load.

---

### 4. Actor

The actor is the second LLM call. It receives:
- The schema snapshot
- Sample rows from the relevant tables (so it sees actual location strings and date formats)
- The plan from the planner
- An explicit **intent hint** derived from the query (`INTENT: find a single extreme-value day. Use ORDER BY ... LIMIT 1.`)
- A **location pin** (`LOADED LOCATION: 'Chicago' — you MUST use WHERE location = 'Chicago'`)
- Any previous failed attempts and error messages (on retry)

The actor outputs raw SQL, which is immediately executed against the local SQLite database. Results are stored in state and passed to the judge.

The intent hint system is a guardrail specifically for small models — it forces the model toward the correct SQL pattern (aggregation vs. point lookup vs. overview) before it generates a single token of SQL.

---

### 5. Judge

The judge evaluates whether the SQL result correctly answers the user's question. It runs two checks in order:

**Programmatic check (no LLM)**

First, deterministic rules catch clear failures without spending an LLM call:
- SQL syntax error → fail with the exact error message
- Zero rows returned → fail with a hint about filter logic

**Semantic check (LLM)**

If the query ran and returned rows, a lightweight LLM call asks: *does this result actually answer the question?* It can only fail for:
- `wrong_aggregation` — used AVG when MIN/MAX was needed
- `missing_filter` — a required filter is absent
- `wrong_table` — queried the wrong table entirely

The judge is deliberately conservative: if the data answers the question, it passes — even if the numbers look surprising. It never fails based on world knowledge.

**Confidence threshold**

A result must pass AND have confidence ≥ 0.85 to be accepted. Below that, the judge treats it as a retry.

---

### 6. Judge → MCP Reload (Recovery Path)

If the SQL returns zero rows on the **second attempt**, the judge checks whether the generated SQL was filtering on a date range that isn't in the database. It extracts dates from the SQL via regex and calls `check_coverage` via MCP:

```
SQL:  WHERE location = 'Chicago' AND date >= '2024-06-01' AND date <= '2024-06-30'
      ↓
check_coverage(location='Chicago', start_date='2024-06-01', end_date='2024-06-30')
      ↓ not covered
      ↓
inject tool_calls into plan → route back to ENRICHER → reload → retry ACTOR
```

This recovery path handles the case where the actor wrote a date-filtered query for a range that the enricher hadn't loaded yet. The agent reloads the exact missing range from Open-Meteo and retries — without involving the LLM planner again.

The loop runs up to **5 iterations** before the agent gives up and returns an honest "no data" response.

---

### 7. Final Answer

The synthesizer (a third LLM call) converts the raw SQL result rows into a concise plain-English answer. It is strictly grounded — it copies numbers and dates exactly from the data and never invents or estimates values.

---

## Data Sources

| Source | API | Coverage |
|---|---|---|
| Open-Meteo Forecast | `api.open-meteo.com/v1/forecast` | Next 16 days |
| Open-Meteo Archive | `archive-api.open-meteo.com/v1/archive` | 1940–yesterday |

No API keys required. Data is fetched on demand and cached in `cityops.sqlite` for the session.

---

## Project Structure

```
Agentic_SQL_Chatbot/
├── chatbot.py            # Entry point — CLI REPL
├── agent.py              # LangGraph graph (planner → enricher → schema → actor → judge → answer)
├── city_data_server.py   # MCP server (plan_data_load, check_coverage, load_weather, load_csv)
├── data_sources.py       # Open-Meteo HTTP fetch logic
├── database.py           # SQLite helpers (schema, sampling, querying)
├── llm_helper.py         # Ollama wrapper
├── cityops.sqlite        # Local database (auto-created, cleared on startup)
└── requirements.txt
```
