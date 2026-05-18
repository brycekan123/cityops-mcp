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

**Supported cities:** Atlanta, Chicago, New York, Los Angeles, Houston, Seattle, Miami, Denver

**Change the model** (optional):
```bash
OLLAMA_MODEL=llama3.2 python chatbot.py
```

---

## Running with Docker

Docker guarantees the MCP subprocess (`city_data_server.py`, spawned via stdio) runs in exactly the right environment regardless of local Python setup.

**First time only** — pull the model into the Ollama container:
```bash
docker compose up ollama -d
docker compose exec ollama ollama pull llama3.1
```

**Start everything:**
```bash
docker compose up --build
docker compose exec chatbot python chatbot.py
```

**Environment variables** (copy `.env.example` → `.env` to override defaults):

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_MODEL` | `llama3.1` | Any model pulled into Ollama |
| `OLLAMA_HOST` | `http://localhost:11434` | Set automatically to `http://ollama:11434` in Compose |

CSV files in `./data/` are mounted into the container so local files are accessible via `load_csv`.

---

## Architecture

| File | Role |
|---|---|
| `chatbot.py` | CLI loop — takes input, calls agent, prints answer + MCP metrics |
| `agent.py` | LangGraph graph — orchestrates the full pipeline |
| `city_data_server.py` | MCP server — Tools, Resources, Prompts, Middleware |
| `mcp_client.py` | Sync MCP client — persistent connection, caching, session metrics |
| `database.py` | SQLite helpers — schema, sampling, querying |
| `llm_helper.py` | Ollama wrapper — single `llm()` call used everywhere |
| `benchmark.py` | Automated MCP benchmark — cache impact, error demo, primitives discovery |

### MCP Protocol Coverage

This project demonstrates all three MCP primitives:

| Primitive | Implementation | Purpose |
|---|---|---|
| **Tools** | `plan_data_load`, `check_coverage`, `load_weather`, `load_csv` | Data fetching and routing decisions |
| **Resources** | `weather://schema`, `weather://tables` | Live DB schema and table inventory |
| **Prompts** | `extreme_value_query`, `trend_overview_query`, `specific_date_query`, `comparison_query`, `aggregation_query` | Server-managed SQL scaffolds injected into the actor before SQL generation |

The client layer adds:
- **Persistent connection** — MCP server subprocess spawns once per session (~750ms saved per subsequent call)
- **Per-tool and per-resource caching** with smart invalidation (location-scoped for `load_weather`, full clear for `load_csv`, prompts never invalidated)
- **SessionMetrics** — p50/p95 latency, cache hit rate, time saved, error rate, retry success rate

The server adds:
- **Middleware** — `ErrorHandlingMiddleware` wraps every tool call; unhandled exceptions (bad city name, API timeout) return `{"error": "..."}` instead of crashing the enricher

---

## Running the Benchmark

`benchmark.py` demonstrates the full MCP protocol in isolation — no LLM required:

```bash
python benchmark.py
```

It clears the database, then runs 5 semantically different questions about the same dataset (Atlanta summer, Chicago forecast). This shows real cache acceleration: Q1 pays the full load cost, Q2–Q3 hit `check_coverage` and `weather://schema` from cache. It also discovers and fetches all three MCP primitives (Tools, Resources, Prompts) and runs an error injection demo showing `ErrorHandlingMiddleware` catching a bad city name and returning a structured dict instead of a crash.

---

## Agentic Workflow

Every query passes through six nodes in a LangGraph state machine.

```
User query
    │
    ▼
┌─────────┐
│ PLANNER │  LLM parses intent and identifies which tables to query
└────┬────┘
     │
     ▼
┌──────────┐ ◄─────────────────────────────────────┐
│ ENRICHER │  MCP: plan → coverage check → load     │ (reload: missing date range)
└────┬─────┘                                        │
     │                                              │
     ▼                                              │
┌────────┐                                          │
│ SCHEMA │  MCP: weather://schema + SQL Prompt       │
└───┬────┘                                          │
    │                                               │
    ▼                                               │
┌───────┐ ◄──────────────────┐                     │
│ ACTOR │  LLM writes SQL     │ (retry, up to 5x)  │
└───┬───┘                    │                     │
    │                        │                     │
    ▼                        │                     │
┌───────┐  fail (bad SQL) ───┘                     │
│ JUDGE │  fail (missing data) ────────────────────┘
└───┬───┘
    │ pass
    ▼
┌──────────────┐
│ FINAL ANSWER │  LLM synthesizes rows into plain English
└──────────────┘
```

**Planner** — Outputs a JSON plan: intent, tables, and strategy. Does not decide what data to load.

**Enricher** — Calls `plan_data_load` to parse the query into a fetch request, `check_coverage` to check if the date range is already in SQLite, and `load_weather` only if data is missing.

**Schema** — Reads the `weather://schema` MCP Resource and fetches a matching MCP Prompt scaffold (one of five SQL templates keyed by query intent: extreme value, overview, specific date, comparison, aggregation). Both are injected into the actor.

**Actor** — Receives schema, sample rows filtered to the loaded city, the plan, a deterministic intent hint, and any prior failed attempts with error messages. Outputs raw SQL, immediately executed against SQLite.

**Judge** — First runs deterministic checks (syntax error → fail, zero rows → fail). If rows returned, a lightweight LLM call checks semantic correctness: wrong aggregation type, result for the wrong entity, or wrong table. Confidence ≥ 0.85 to pass. On fail: routes back to the actor (up to 5 retries) or back to the enricher if the SQL asked for a date range not yet in the DB.

**Final Answer** — Converts result rows into a plain-English answer grounded strictly in the returned data.

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
