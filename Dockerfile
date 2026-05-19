FROM python:3.11-slim

WORKDIR /app

# Install uv for fast, lockfile-aware deps
RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src

# Install the package + the agent demo extras (Ollama + LangGraph)
RUN uv pip install --system --no-cache ".[agent]"

# Copy the rest (chatbot.py, agent.py, mcp_client.py, etc.)
COPY . .

CMD ["python", "chatbot.py"]
