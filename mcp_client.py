"""
Sync MCP client wrapper.

Connects to city_data_server.py via stdio (spawned once as a subprocess).
All public functions are synchronous so LangGraph nodes can call them directly.

Includes:
  - Client-side cache for pure tools (plan_data_load, check_coverage)
  - SessionMetrics: p95 latency, cache hit rate, error rate, avg calls/query
  - read_resource(): reads MCP Resources (weather://schema, weather://tables)
"""

import asyncio
import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

SERVER_SCRIPT = Path(__file__).parent / "city_data_server.py"

# ── Persistent MCP connection ─────────────────────────────────────────────────
# One server subprocess, one event loop thread, alive for the whole session.
# The client lives inside an async with block held open by an infinite sleep —
# this keeps the stdio transport connected. Spawning per-call costs ~750ms;
# the persistent connection drops subsequent calls to ~50ms.

_loop:   asyncio.AbstractEventLoop | None = None
_client: Client | None                   = None
_lock  = threading.Lock()
_ready = threading.Event()


def _ensure_connection() -> None:
    global _loop, _client
    if _client is not None:
        return
    with _lock:
        if _client is not None:
            return

        _loop = asyncio.new_event_loop()

        # Start the event loop in a background thread so it runs forever
        thread = threading.Thread(target=_loop.run_forever, daemon=True)
        thread.start()

        async def _keep_alive():
            global _client
            try:
                async with Client(PythonStdioTransport(script_path=SERVER_SCRIPT)) as c:
                    _client = c
                    _ready.set()
                    # Hold the async-with context open for the session lifetime
                    while True:
                        await asyncio.sleep(3600)
            except Exception as e:
                print(f"[MCP] connection error: {e}", flush=True)
                _ready.set()  # unblock main thread even on failure

        # Schedule the keep-alive coroutine on the running loop
        asyncio.run_coroutine_threadsafe(_keep_alive(), _loop)

        connected = _ready.wait(timeout=30)
        if connected and _client is not None:
            print(f"[MCP] connected to {SERVER_SCRIPT.name} (persistent session)")
        else:
            print(f"[MCP] WARNING: connection timed out — falling back to per-call mode")


def _run(coro):
    """Submit a coroutine to the persistent event loop and block for the result."""
    _ensure_connection()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()

# Tools whose results are safe to cache within a session (pure / read-only)
_CACHEABLE_TOOLS = {"plan_data_load", "check_coverage"}
# Resources whose content is stable until load_weather changes DB state
_CACHEABLE_RESOURCES = {"weather://schema"}
_tool_cache: dict[str, Any] = {}


def _cache_key(tool_name: str, args: dict) -> str:
    return f"{tool_name}:{json.dumps(args, sort_keys=True)}"


def _invalidate_cache(location: str | None = None) -> None:
    """
    Selectively invalidate cache after DB state changes.
      load_weather(location): clears only coverage/plan entries for that city.
        Schema and prompts are preserved — both are independent of row data.
      load_csv (location=None): clears schema + coverage but preserves prompts
        (prompts are pure SQL templates, never depend on DB content).
    """
    schema_key = "resource:weather://schema"
    if location is None:
        to_delete = [k for k in _tool_cache
                     if not k.startswith("prompt:")]
        for k in to_delete:
            del _tool_cache[k]
        return
    loc_lower = location.lower()
    to_delete = [
        k for k in _tool_cache
        if k != schema_key
        and not k.startswith("prompt:")
        and loc_lower in k.lower()
    ]
    for k in to_delete:
        del _tool_cache[k]


# ── Session Metrics ───────────────────────────────────────────────────────────

class SessionMetrics:
    """
    Tracks MCP performance across a session.

    Metrics:
      p95 latency      — per tool, excludes cache hits
      cache hit rate   — for plan_data_load + check_coverage
      tool error rate  — errors / total non-cached calls
      avg calls/query  — total MCP calls (tools + resources) per query
      retry success    — judge retries that recovered to a passing answer
    """

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._query_call_counts: list[int] = []
        self._current_query_calls = 0
        self._retries: list[tuple[int, bool]] = []

    def _bucket(self, name: str) -> dict:
        if name not in self._tools:
            self._tools[name] = {"latencies": [], "errors": 0, "cache_hits": 0}
        return self._tools[name]

    def record(self, name: str, latency_ms: float,
               cached: bool = False, error: bool = False) -> None:
        b = self._bucket(name)
        if cached:
            b["cache_hits"] += 1
        else:
            b["latencies"].append(latency_ms)
            if error:
                b["errors"] += 1
        self._current_query_calls += 1

    def begin_query(self) -> None:
        self._current_query_calls = 0

    def end_query(self, iterations: int, success: bool) -> None:
        self._query_call_counts.append(self._current_query_calls)
        if iterations > 1:
            self._retries.append((iterations, success))

    def report(self) -> str:
        W = 60
        lines = ["", f"{'─' * W}", " MCP Session Metrics", f"{'─' * W}"]

        lines.append(f"  {'Tool / Resource':<28} {'p50':>6} {'p95':>6} {'cache':>10} {'err':>4}")
        lines.append(f"  {'─' * 56}")

        time_saved_ms = 0.0
        for name, data in sorted(self._tools.items()):
            lats  = data["latencies"]
            hits  = data["cache_hits"]
            errs  = data["errors"]
            total = len(lats) + hits

            if not total:
                continue

            if lats:
                p50 = statistics.median(lats)
                p95 = sorted(lats)[max(0, int(len(lats) * 0.95) - 1)]
                lat_str = f"{p50:>5.0f}ms {p95:>5.0f}ms"
                if hits:
                    time_saved_ms += hits * statistics.mean(lats)
            else:
                lat_str = f"  {'—':>5}    {'—':>5}"

            cache_str = f"{hits}/{total} ({100 * hits // total}%)" if hits else "—"
            err_str   = str(errs) if errs else "—"

            lines.append(f"  {name:<28} {lat_str}  {cache_str:>10}  {err_str:>4}")

        lines.append(f"  {'─' * 56}")

        if self._query_call_counts:
            avg = sum(self._query_call_counts) / len(self._query_call_counts)
            n   = len(self._query_call_counts)
            lines.append(f"  avg MCP calls / query : {avg:.1f}  ({n} {'query' if n == 1 else 'queries'} this session)")

        total_calls = sum(len(d["latencies"]) + d["cache_hits"] for d in self._tools.values())
        total_hits  = sum(d["cache_hits"] for d in self._tools.values())
        if total_calls:
            lines.append(f"  overall cache hit rate : {total_hits}/{total_calls} ({100 * total_hits // total_calls}%)")

        if time_saved_ms > 0:
            lines.append(f"  time saved by cache   : ~{time_saved_ms:.0f}ms")

        total_real  = sum(len(d["latencies"]) for d in self._tools.values())
        total_errs  = sum(d["errors"] for d in self._tools.values())
        if total_real:
            lines.append(f"  tool error rate        : {total_errs}/{total_real} ({100 * total_errs // total_real}%)")

        if self._retries:
            succeeded = sum(1 for _, ok in self._retries if ok)
            lines.append(f"  retry success rate     : {succeeded}/{len(self._retries)}")

        lines.append(f"{'─' * W}")
        return "\n".join(lines)


metrics = SessionMetrics()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract(result) -> dict:
    """Pull structured content from a CallToolResult."""
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    for block in result.content:
        if hasattr(block, "text"):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"text": block.text}
    return {}


def _fmt(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"
    if "files" in result:
        return str(result["files"])
    if "preview_rows" in result:
        cols = result.get("columns", [])
        n    = len(result.get("preview_rows", []))
        return f"{len(cols)} columns: {', '.join(cols)} | {n} preview rows shown"
    if "table_name" in result:
        dr = f" [{result['date_range']}]" if result.get("date_range") else ""
        return f"loaded {result['row_count']} rows → table '{result['table_name']}'{dr}"
    return str(result)


# ── Public API ────────────────────────────────────────────────────────────────

def list_mcp_tools() -> list[str]:
    """Return available tool names from the MCP server."""
    async def _call():
        tools = await _client.list_tools()
        return [t.name for t in tools]

    names = _run(_call())
    print(f"[MCP] list_tools → {names}")
    return names


def call_tool(tool_name: str, args: dict) -> dict:
    """
    Call a named MCP tool and return its result as a dict.

    plan_data_load and check_coverage are client-cached within the session.
    load_weather invalidates the cache (it changes DB state).
    All calls are recorded in SessionMetrics.
    """
    key = _cache_key(tool_name, args)
    if tool_name in _CACHEABLE_TOOLS and key in _tool_cache:
        metrics.record(tool_name, 0.0, cached=True)
        print(f"[MCP] {tool_name:<22} → cache hit")
        return _tool_cache[key]

    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"[MCP] calling tool → {tool_name}({arg_str})")

    t0 = time.perf_counter()

    async def _call():
        return await _client.call_tool(tool_name, args)

    raw     = _run(_call())
    result  = _extract(raw)
    elapsed = (time.perf_counter() - t0) * 1000

    is_error = "error" in result
    metrics.record(tool_name, elapsed, error=is_error)
    print(f"[MCP] {tool_name:<22} → {_fmt(result)}  [{elapsed:.0f}ms]")

    if tool_name in _CACHEABLE_TOOLS and not is_error:
        _tool_cache[key] = result
    elif tool_name == "load_weather" and not is_error:
        _invalidate_cache(location=args.get("location", ""))
    elif tool_name == "load_csv" and not is_error:
        _invalidate_cache()  # full clear — new table means schema changed

    return result


def list_prompts() -> list[str]:
    """Return available prompt names from the MCP server."""
    async def _call():
        prompts = await _client.list_prompts()
        return [p.name for p in prompts]

    try:
        names = _run(_call())
        print(f"[MCP] list_prompts   → {names}")
        return names
    except Exception as e:
        print(f"[MCP] list_prompts   → ERROR: {e}")
        return []


def get_prompt(name: str, args: dict) -> str:
    """
    Fetch a rendered MCP Prompt and return its text content.
    Prompts are always cached — they are pure SQL templates independent of DB state.
    Recorded in SessionMetrics under 'prompt:<name>'.
    """
    cache_key = f"prompt:{name}:{json.dumps(args, sort_keys=True)}"
    if cache_key in _tool_cache:
        metrics.record(f"prompt:{name}", 0.0, cached=True)
        print(f"[MCP] prompt        → {name}  (cache hit)")
        return _tool_cache[cache_key]

    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"[MCP] prompt        → {name}({arg_str})")
    t0 = time.perf_counter()

    async def _call():
        result = await _client.get_prompt(name, arguments=args)
        return "\n".join(
            msg.content.text for msg in result.messages
            if hasattr(msg.content, "text")
        )

    try:
        text    = _run(_call())
        elapsed = (time.perf_counter() - t0) * 1000
        metrics.record(f"prompt:{name}", elapsed)
        print(f"[MCP] prompt        → {len(text)} chars  [{elapsed:.0f}ms]")
        _tool_cache[cache_key] = text
        return text
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        metrics.record(f"prompt:{name}", elapsed, error=True)
        print(f"[MCP] prompt        → ERROR: {e}  [{elapsed:.0f}ms]")
        return ""


def list_resources() -> list[str]:
    """Return available resource URIs from the MCP server."""
    async def _call():
        resources = await _client.list_resources()
        return [str(r.uri) for r in resources]

    try:
        uris = _run(_call())
        print(f"[MCP] list_resources → {uris}")
        return uris
    except Exception as e:
        print(f"[MCP] list_resources → ERROR: {e}")
        return []


def read_resource(uri: str) -> str:
    """
    Read an MCP Resource by URI and return its text content.
    weather://schema is cached client-side and invalidated on load_weather.
    Recorded in SessionMetrics under the URI name.
    """
    cache_key = f"resource:{uri}"
    if uri in _CACHEABLE_RESOURCES and cache_key in _tool_cache:
        metrics.record(uri, 0.0, cached=True)
        print(f"[MCP] resource      → {uri}  (cache hit)")
        return _tool_cache[cache_key]

    print(f"[MCP] resource      → {uri}")

    t0 = time.perf_counter()

    async def _call():
        contents = await _client.read_resource(uri)
        return contents[0].text if contents else ""

    try:
        text    = _run(_call())
        elapsed = (time.perf_counter() - t0) * 1000
        metrics.record(uri, elapsed)
        print(f"[MCP] resource      → {len(text)} chars  [{elapsed:.0f}ms]")
        if uri in _CACHEABLE_RESOURCES:
            _tool_cache[cache_key] = text
        return text
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        metrics.record(uri, elapsed, error=True)
        print(f"[MCP] resource      → ERROR: {e}  [{elapsed:.0f}ms]")
        return ""
