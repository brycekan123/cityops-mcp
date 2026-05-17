#!/usr/bin/env python3
"""
Agentic SQL Chatbot — interactive CLI.
Run: python chatbot.py
Requires: Ollama running locally (ollama serve)
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "cityops.sqlite"


BANNER = """
╔══════════════════════════════════════════════════════════╗
║        Weather Agent  (Ollama + LangGraph + MCP)         ║
║  Powered by Open-Meteo — forecast + historical archive   ║
║  Cities: Atlanta, Chicago, NYC, LA, Houston, Seattle,    ║
║          Miami, Denver    •   quit / exit / q to stop    ║
╚══════════════════════════════════════════════════════════╝

── Try these queries ────────────────────────────────────────
  what's the forecast for Atlanta this week?
  hottest day last summer in Chicago?
  what was it like in Los Angeles in August 2024?
  which days had rain last month in Seattle?
  coldest night in New York last January?
  will it rain in Miami tomorrow?
"""

SEPARATOR = "─" * 60


def clear_weather_data() -> None:
    """Clear all weather_daily rows on startup so every session fetches fresh data."""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH))
        deleted = conn.execute("DELETE FROM weather_daily").rowcount
        conn.commit()
        conn.close()
        if deleted:
            print(f"[startup] cleared {deleted} weather rows — will fetch fresh data this session")
    except Exception:
        pass


def check_ollama() -> None:
    try:
        import ollama
        ollama.list()
    except Exception:
        print("ERROR: Cannot reach Ollama. Is it running?")
        print("  Start it with:  ollama serve")
        sys.exit(1)


def print_answer(result: dict) -> None:
    print(f"\n{SEPARATOR}")
    status = result.get("status", "unknown")
    iters  = result.get("iteration", 0)
    answer = result.get("final_answer", "No answer generated.")
    print(f"Status: {status}  |  Iterations used: {iters}")
    print(f"\nAnswer:\n{answer}")
    print(SEPARATOR)


def main() -> None:
    check_ollama()
    clear_weather_data()

    try:
        import agent
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(BANNER)

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not query:
            continue

        if query.lower() in {"quit", "exit", "q"}:
            print("Goodbye!")
            break

        try:
            result = agent.run(query)
            print_answer(result)
        except Exception as e:
            print(f"\nERROR: {e}\n")


if __name__ == "__main__":
    main()
