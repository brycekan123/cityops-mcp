# cityops-mcp

A Model Context Protocol (MCP) server that gives Claude (or any MCP client) live weather data for 8 US cities — forecast *and* historical, no API keys, no setup beyond pasting one command.

```
hottest day last summer in Chicago?
will it rain in Miami tomorrow?
what's the forecast for Atlanta this week?
coldest night in Seattle last January?
```

Supports: Atlanta, Chicago, New York, Los Angeles, Houston, Seattle, Miami, Denver.

---

## ⚡ Install in Claude Code (one command)

On a clean laptop, this is the whole install. No `git clone` needed — `uvx` clones the repo, builds an isolated venv, and runs the server for you.

**1. Install `uv`** (skip if already installed):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Register the server with Claude Code:**

```bash
claude mcp add cityops -s user -- uvx --from git+https://github.com/brycekan123/MCP_Agent_weather cityops-mcp
```

**3. Verify:**

```bash
claude mcp list
```

You should see `cityops: uvx --from git+https://… cityops-mcp - ✓ Connected`.

**4. Open a new Claude Code session and ask:** *"hottest day in Atlanta last summer?"*

First call takes ~30s while `uv` builds the package. After that, fast.

> Paste the `claude mcp add` command on **one line**. Terminal soft-wraps break it and silently register a truncated entry. If that happens: `claude mcp remove cityops -s user` and try again.

---

## Install in Claude Desktop

Paste this into your config file (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS; `%APPDATA%\Claude\claude_desktop_config.json` on Windows; `~/.config/Claude/claude_desktop_config.json` on Linux):

```json
{
  "mcpServers": {
    "cityops": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/brycekan123/MCP_Agent_weather",
        "cityops-mcp"
      ]
    }
  }
}
```

Quit Claude Desktop completely (⌘Q), reopen, and ask the same question.

If Claude Desktop can't find `uvx`, use the absolute path from `which uvx` (Claude Desktop spawns subprocesses with a stripped PATH).

---

## 🌐 Other MCP clients

cityops-mcp speaks the standard MCP protocol over stdio — the same wire format every MCP client supports. The same `uvx --from … cityops-mcp` command works in:

- **Cursor** — drop the JSON snippet into Cursor's MCP settings.
- **Continue** — same JSON snippet, see [Continue's MCP docs](https://docs.continue.dev/customize/deep-dives/mcp).
- **MCP Inspector** (browser debug GUI) — `npx @modelcontextprotocol/inspector -- uvx --from git+https://github.com/brycekan123/MCP_Agent_weather cityops-mcp`
- **Your own agent** — use [FastMCP's Client](https://github.com/jlowin/fastmcp) or the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

The LLM behind the client doesn't matter — Claude, GPT, local Llama, whatever your client supports.

---

## 🧰 What it does

| Tool | Purpose |
|---|---|
| `plan_data_load` | Parses a natural-language query into Open-Meteo fetch parameters (city + date range) |
| `check_coverage` | Reports whether the requested city/date range is already in the local DB |
| `load_weather` | Fetches from Open-Meteo (forecast or archive) and stores rows in `weather_daily` |
| `load_csv` | Loads an arbitrary CSV from the data directory into a new SQLite table |
| `run_sql` | Executes a read-only `SELECT`/`WITH` query against loaded tables (capped at 1000 rows) |
| `get_loaded_tables` | Returns current tables + columns + row counts |
| `list_sources` | Lists the configured public data sources |

Plus 2 resources (`weather://schema`, `weather://tables`) and 5 SQL-scaffold prompts (`extreme_value_query`, `trend_overview_query`, `specific_date_query`, `comparison_query`, `aggregation_query`).

### Coverage-aware caching

Repeat questions about the same city/date range skip the Open-Meteo round-trip:

- **First** — *"hottest day in Atlanta last summer?"* → `check_coverage` (false) → `load_weather` (94 days) → `run_sql`
- **Second** — *"coldest night in Atlanta last summer?"* → `check_coverage` (**true**) → skips `load_weather` → `run_sql` directly

The cache is the local SQLite file (`cityops.sqlite` in your platform's user-data dir) — persistent across sessions. Delete the file to start fresh.

---

## Troubleshooting

**"Server not showing up."** macOS Claude Desktop strips PATH when spawning subprocesses, so bare `"command": "uvx"` may fail silently. Use the absolute path from `which uvx`. For Claude Code, see [the `claude mcp` line above](#-install-in-claude-code-one-command) — `uvx` is on its PATH already.

**"First launch is taking forever."** Expected — `uvx` is cloning the repo and building a venv. 30–60 seconds is normal. Subsequent launches use the cache.

**"Where are the server logs?"**
- macOS: `~/Library/Logs/Claude/mcp-server-cityops.log`
- Windows: `%APPDATA%\Claude\logs\mcp-server-cityops.log`
- Linux: `~/.config/Claude/logs/mcp-server-cityops.log`

Set `CITYOPS_LOG_LEVEL=DEBUG` in the config's `env` block for verbose output.

**"Corporate firewall blocks GitHub clone."** Fall back to a local install:

```bash
git clone https://github.com/brycekan123/MCP_Agent_weather ~/cityops-mcp
claude mcp add cityops -s user -- $(which uvx) --from ~/cityops-mcp cityops-mcp
```

---

## What's NOT included

- No authentication — stdio runs locally as a child process.
- No upstream API keys — Open-Meteo is free and keyless.
- No HTTP transport — local stdio only.

---

## License

MIT — see [LICENSE](LICENSE).

Built on [FastMCP](https://github.com/jlowin/fastmcp). Data from [Open-Meteo](https://open-meteo.com/).
