# cityops-mcp

[![CI](https://github.com/brycekan/cityops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/brycekan/cityops-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Model Context Protocol (MCP) server that gives Claude (or any MCP client) live weather data for 8 US cities — forecast *and* historical, no API keys, no setup beyond pasting a config snippet.

```
hottest day last summer in Chicago?
will it rain in Miami tomorrow?
what's the forecast for Atlanta this week?
coldest night in Seattle last January?
```

Supports: Atlanta, Chicago, New York, Los Angeles, Houston, Seattle, Miami, Denver.

---

## ⚡ Install in Claude Desktop in 30 seconds

**1. Install `uv`** (if you don't already have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Paste this into your Claude Desktop config:**

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "cityops": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/brycekan/cityops-mcp",
        "cityops-mcp"
      ]
    }
  }
}
```

**3. Quit and reopen Claude Desktop.** Ask it: *"hottest day in Atlanta last summer?"*

That's the whole install. First launch takes 30–60s while `uv` clones, builds a venv, and installs deps. Subsequent launches are fast.

---

## Install in Claude Code in one command

If you use [Claude Code](https://docs.anthropic.com/claude/docs/claude-code), the CLI handles registration directly — no JSON editing.

```bash
claude mcp add cityops -s user -- uvx --from git+https://github.com/brycekan/cityops-mcp cityops-mcp
```

Verify:

```bash
claude mcp list
```

You should see a line like `cityops: uvx --from git+https://… cityops-mcp - ✓ Connected`. Open a new Claude Code session and ask the same question.

> **Heads up — one-line gotcha.** Paste the `claude mcp add` command as a single line. If your terminal soft-wraps it across two lines and you press return mid-command, only the part before the break registers — you'll end up with a truncated, broken entry (`uvx --from` and nothing else) that fails silently with `✗ Failed to connect`. If that happens, `claude mcp remove cityops -s user` and try again on one line.

For local development against a checked-out copy, point `--from` at the directory instead of the git URL, and use the **absolute path** to `uvx` (Claude Code's MCP env doesn't inherit your shell's PATH):

```bash
claude mcp add cityops -s user -- $(which uvx) --from /absolute/path/to/cityops-mcp cityops-mcp
```

---

## 🌐 Works with any MCP client

cityops-mcp speaks the standard MCP protocol over stdio — the same wire format every MCP client supports. Pick yours:

| Client | Status | How to install |
|---|---|---|
| **Claude Desktop** (macOS/Windows/Linux) | Verified ✓ | JSON config snippet — [see above](#-install-in-claude-desktop-in-30-seconds) |
| **Claude Code** (CLI + Desktop) | Verified ✓ | `claude mcp add` — [see above](#install-in-claude-code-in-one-command) |
| **MCP Inspector** (browser debug GUI) | Verified ✓ | `npx @modelcontextprotocol/inspector -- uvx --from git+https://github.com/brycekan/cityops-mcp cityops-mcp` |
| **Cursor** | Same JSON format as Claude Desktop | Drop the snippet into Cursor's MCP settings |
| **Continue** | Same JSON format as Claude Desktop | See [Continue's MCP docs](https://docs.continue.dev/customize/deep-dives/mcp) |
| **Your own agent** | Verified ✓ | Use [FastMCP's Client](https://github.com/jlowin/fastmcp) or the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). `mcp_client.py` in this repo is a working example with session metrics. |

The LLM behind the client is decoupled from the server — Claude, GPT-4, local Llama, whatever your client supports works the same way over the same protocol.

---

## 🧰 What it does

When connected, your MCP client gains the following capabilities. The LLM decides when to call each one — you just ask questions in plain English.

### Tools

| Tool | Purpose |
|---|---|
| `plan_data_load` | Parses a natural-language query into Open-Meteo fetch parameters (city + date range) |
| `check_coverage` | Reports whether the requested city/date range is already in the local DB |
| `load_weather` | Fetches from Open-Meteo (forecast or archive) and stores rows in `weather_daily` |
| `load_csv` | Loads an arbitrary CSV from the data directory into a new SQLite table |
| `run_sql` | Executes a read-only SELECT/WITH query against the loaded tables and returns rows (capped at 1000). Pairs with the prompts. |
| `get_loaded_tables` | Returns current tables + columns + row counts |
| `list_sources` | Lists the configured public data sources |

### Resources

| URI | Content |
|---|---|
| `weather://schema` | Live database schema (tables + columns) |
| `weather://tables` | Currently loaded tables with row counts |

### Prompts (server-managed SQL scaffolds)

| Name | When to use |
|---|---|
| `extreme_value_query` | Hottest/coldest/windiest/wettest single day |
| `trend_overview_query` | Multi-day summary with date range guidance |
| `specific_date_query` | Today / tomorrow / yesterday / a named date |
| `comparison_query` | City vs city aggregates |
| `aggregation_query` | Averages, totals, counts |

### Coverage-aware caching

Most weather questions about the same city/date range don't need a fresh Open-Meteo round-trip. The server exposes `check_coverage` so the LLM can ask *"is this already loaded?"* before calling `load_weather`. In practice:

- **First query** in a session — *"hottest day in Atlanta last summer?"* → `plan_data_load` → `check_coverage` (returns `false`) → `load_weather` (fetches ~94 days) → `run_sql` to find the max.
- **Second query** in the same session — *"coldest night in Atlanta last summer?"* → `plan_data_load` → `check_coverage` (returns **`true`**) → skips `load_weather` entirely → `run_sql` straight against the rows the first query already pulled.

The cache is the local SQLite file (`cityops.sqlite` in your platform's user-data dir) — persistent across sessions, accumulating coverage as you ask different questions. No TTL; delete the file to start fresh.

---

## Architecture

```
                 ┌──────────────────────────────┐
 Claude /        │       MCP client             │
 Cursor /  ───►  │  (Claude Desktop, Cursor,    │
 Continue        │   Continue, your own agent)  │
                 └──────────────┬───────────────┘
                                │ stdio (JSON-RPC)
                                ▼
                 ┌──────────────────────────────┐
                 │      cityops-mcp server      │
                 │  (FastMCP + middleware)      │
                 ├──────────────────────────────┤
                 │  Tools • Resources • Prompts │
                 └─────────────┬────────────────┘
                               │
              ┌────────────────┴───────────────┐
              ▼                                ▼
     ┌────────────────┐              ┌──────────────────┐
     │   Open-Meteo   │              │   cityops.sqlite │
     │ (forecast +    │              │   (session DB)   │
     │  archive APIs) │              │                  │
     └────────────────┘              └──────────────────┘
```

The server speaks the standard Model Context Protocol over stdio. It runs as a child process of the MCP client — no ports, no auth, no hosted service.

---

## Local development

```bash
git clone https://github.com/brycekan/cityops-mcp
cd cityops-mcp
uv sync --extra dev

# Run the server directly (rare — usually you connect via a client)
uv run cityops-mcp

# Inspect the server in a browser GUI — the canonical MCP debugging tool
npx @modelcontextprotocol/inspector -- uv run cityops-mcp

# Run tests
uv run pytest

# Lint
uv run ruff check src tests
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CITYOPS_DB_PATH` | Platform user data dir | Override the SQLite file location |
| `CITYOPS_DATA_DIR` | `<user-data-dir>/csv` | Directory `load_csv` reads from |
| `CITYOPS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Troubleshooting

### "Server not showing up in Claude Desktop"

Most common cause: **`uvx` is not on Claude Desktop's PATH.** macOS Claude Desktop spawns subprocesses with a stripped environment, so `command: "uvx"` may fail silently even when `which uvx` works in your terminal.

Fix: use the absolute path. In a terminal, run `which uvx` and put the full path into the config:

```json
"command": "/Users/<you>/.local/bin/uvx",
```

### "First launch is taking forever"

Expected — `uvx` is cloning the repo and building a venv on the first run. 30–60 seconds is normal. Subsequent launches use the cache.

### "Where are the server logs?"

Claude Desktop writes per-server logs to:

- macOS: `~/Library/Logs/Claude/mcp-server-cityops.log`
- Windows: `%APPDATA%\Claude\logs\mcp-server-cityops.log`
- Linux: `~/.config/Claude/logs/mcp-server-cityops.log`

For more verbose output set `CITYOPS_LOG_LEVEL=DEBUG` in the config's `env` block.

### "Got a JSON syntax error"

Validate the config file at [jsonlint.com](https://jsonlint.com). Common gotchas: missing commas between server entries, trailing commas, mismatched braces.

### "Corporate firewall blocks GitHub clone"

Fall back to a local install:

```bash
git clone https://github.com/brycekan/cityops-mcp ~/cityops-mcp
cd ~/cityops-mcp && uv sync
```

Then change the config `command` to point at the local install:

```json
"command": "uv",
"args": ["run", "--directory", "/Users/<you>/cityops-mcp", "cityops-mcp"]
```

---

## What's NOT included

This server is intentionally minimal:
- No authentication — stdio transport runs locally as a child process; no network surface to secure.
- No upstream API keys — Open-Meteo is free and keyless.
- No HTTP transport — local stdio only. (Hosted/remote MCP is on the roadmap.)
- No multi-server orchestration — this is one focused server. (Companion servers for air quality and astronomy are planned.)

---

## Bonus: the in-repo agent demo

This repository also contains a single-agent LangGraph chatbot that uses `cityops-mcp` as its data layer — included for reference and as an example of how to build agents that consume MCP servers.

```bash
uv sync --extra dev --extra agent
ollama pull llama3.1   # the demo uses local Ollama
uv run python chatbot.py
```

See `agent.py` for the planner → enricher → schema → actor → judge graph, and `mcp_client.py` for a persistent-connection MCP client wrapper with session metrics.

---

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built on [FastMCP](https://github.com/jlowin/fastmcp), the official Anthropic Python SDK for MCP. Data from [Open-Meteo](https://open-meteo.com/) (free, no key required, generous rate limits).
