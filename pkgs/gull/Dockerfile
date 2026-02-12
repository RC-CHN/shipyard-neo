# Gull runtime container
# - Python + FastAPI wrapper
# - agent-browser CLI (Node.js)
# - Playwright Chromium + Linux deps

FROM ghcr.io/astral-sh/uv:0.5.30-python3.11-bookworm-slim

WORKDIR /app

# --- System deps + Node.js ---
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# --- agent-browser + Playwright Chromium ---
# 1. Install agent-browser globally
# 2. Install Playwright as a local project dependency so `npx playwright` works
# 3. Install Chromium browser + system deps
# We keep PLAYWRIGHT_BROWSERS_PATH at default (/root/.cache/ms-playwright) so
# agent-browser can find the Chromium binary at runtime.
RUN npm install -g agent-browser \
  && npm init -y \
  && npm install playwright \
  && npx playwright install --with-deps chromium \
  && rm -rf /tmp/* /var/lib/apt/lists/*

# --- Python deps (uv) ---
# Keep README.md in build context because pyproject.toml declares it as the project readme.
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY skills ./skills

RUN uv sync --frozen --no-dev

# Entrypoint: injects built-in skills into /workspace/skills/ before starting
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Runtime defaults
ENV BAY_WORKSPACE_PATH=/workspace

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]

# Use uv to run within the synced environment.
# --loop asyncio: Force standard asyncio event loop instead of uvloop.
# uvloop (installed via uvicorn[standard]) has compatibility issues with
# Node.js child processes (agent-browser): pipe management differs from
# standard asyncio, causing process.communicate() to hang waiting for EOF,
# which manifests as command timeouts.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "asyncio"]
