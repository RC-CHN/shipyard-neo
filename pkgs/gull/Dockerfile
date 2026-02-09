# syntax=docker/dockerfile:1

# Gull runtime container
# - Python + FastAPI wrapper
# - agent-browser CLI (Node.js)
# - Playwright Chromium + Linux deps via `agent-browser install --with-deps`

FROM ghcr.io/astral-sh/uv:0.5.30-python3.11-bookworm-slim

WORKDIR /app

# --- System deps + Node.js + agent-browser ---
# Notes:
# - agent-browser is distributed via npm
# - `agent-browser install --with-deps` downloads Chromium and installs required Linux deps
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && npm install -g agent-browser \
  && agent-browser install --with-deps \
  && apt-get purge -y --auto-remove curl gnupg \
  && rm -rf /var/lib/apt/lists/*

# --- Python deps (uv) ---
COPY pyproject.toml uv.lock README.md ./
COPY app ./app

RUN uv sync --frozen --no-dev

# Runtime defaults
ENV BAY_WORKSPACE_PATH=/workspace

EXPOSE 8080

# Use uv to run within the synced environment
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
