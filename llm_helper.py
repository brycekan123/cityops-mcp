import os
import re

import ollama

MODEL        = os.getenv("OLLAMA_MODEL", "llama3.1")
_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
_client      = ollama.Client(host=_OLLAMA_HOST)


def llm(system: str, user: str, json_mode: bool = False) -> str:
    response = _client.chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        format="json" if json_mode else "",
        options={"temperature": 0.1},
    )
    return response.message.content.strip()


def extract_sql(text: str) -> str:
    text = re.sub(r"```(?:sql)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    # Fix llama3.1 bug: missing closing quote before SQL keywords
    # e.g.  '2025-08-31 ORDER  →  '2025-08-31' ORDER
    text = re.sub(
        r"'(\d{4}-\d{2}-\d{2})\s+(ORDER|GROUP|LIMIT|HAVING|UNION|WHERE|AND|OR)",
        r"'\1' \2",
        text,
        flags=re.IGNORECASE,
    )
    return text
