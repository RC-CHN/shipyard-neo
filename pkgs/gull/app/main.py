"""Gull - Browser Runtime REST Wrapper.

A thin HTTP wrapper around agent-browser CLI, providing:
- POST /exec: Execute any agent-browser command
- GET /health: Health check
- GET /meta: Runtime metadata and capabilities

Architecture:
- Uses CLI passthrough mode: agent-browser commands are passed as strings
- Automatically injects --session and --profile parameters
- --session: mapped to SANDBOX_ID, isolates browser instances
- --profile: mapped to /workspace/.browser/profile/, persists browser state
  (cookies, localStorage, IndexedDB, service workers, cache) across
  container restarts. Cleaned up when Sandbox is deleted (Cargo Volume).
- Uses asyncio.create_subprocess_exec for non-blocking execution
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

# Configuration from environment
SESSION_NAME = os.environ.get("SANDBOX_ID", os.environ.get("BAY_SANDBOX_ID", "default"))
WORKSPACE_PATH = os.environ.get("BAY_WORKSPACE_PATH", "/workspace")
# Persistent browser profile directory on shared Cargo Volume.
# agent-browser --profile automatically persists cookies, localStorage,
# IndexedDB, service workers, and cache to this directory.
BROWSER_PROFILE_DIR = os.path.join(WORKSPACE_PATH, ".browser", "profile")
GULL_VERSION = "0.1.0"


class ExecRequest(BaseModel):
    """Request to execute an agent-browser command."""
    cmd: str = Field(..., description="agent-browser command (without 'agent-browser' prefix)")
    timeout: int = Field(default=30, description="Timeout in seconds", ge=1, le=300)


class ExecResponse(BaseModel):
    """Response from command execution."""
    stdout: str
    stderr: str
    exit_code: int


class HealthResponse(BaseModel):
    """Health check response."""
    status: str  # healthy | degraded | unhealthy
    browser_active: bool
    session: str


class MetaResponse(BaseModel):
    """Runtime metadata response."""
    runtime: dict
    workspace: dict
    capabilities: dict


async def _run_agent_browser(
    cmd: str,
    *,
    timeout: int = 30,
    session: str | None = None,
    profile: str | None = None,
    cwd: str = WORKSPACE_PATH,
) -> tuple[str, str, int]:
    """Execute an agent-browser command via subprocess.

    Automatically injects --session (for browser isolation) and --profile
    (for persistent state on Cargo Volume) parameters.

    Args:
        cmd: Command string (without 'agent-browser' prefix)
        timeout: Timeout in seconds
        session: Session name for browser isolation
        profile: Profile directory for persistent browser state
        cwd: Working directory

    Returns:
        Tuple of (stdout, stderr, exit_code)
    """
    # Build full command with session + profile injection
    parts = ["agent-browser"]
    if session:
        parts.extend(["--session", session])
    if profile:
        parts.extend(["--profile", profile])

    # Use shlex.split to preserve quoted arguments.
    # Example: fill @e1 "hello world"
    parts.extend(shlex.split(cmd))

    try:
        process = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Kill the process on timeout
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
            return "", f"Command timed out after {timeout}s", -1

        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            process.returncode or 0,
        )

    except FileNotFoundError:
        return "", "agent-browser not found. Is it installed?", -1
    except Exception as e:
        return "", f"Failed to execute command: {e}", -1


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager.

    On startup:
    - Ensure browser profile directory exists on Cargo Volume.
    - agent-browser --profile automatically restores persisted state
      (cookies, localStorage, etc.) on first command.

    On shutdown:
    - Close the browser session. agent-browser --profile automatically
      persists state to the profile directory.
    """
    # Ensure profile dir exists on shared Cargo Volume
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    print(f"[gull] Starting Gull v{GULL_VERSION}, session={SESSION_NAME}")
    print(f"[gull] Browser profile dir: {BROWSER_PROFILE_DIR}")

    yield

    # Shutdown: close browser (profile auto-persists state)
    print("[gull] Shutting down, closing browser...")
    await _run_agent_browser(
        "close",
        session=SESSION_NAME,
        profile=BROWSER_PROFILE_DIR,
        timeout=5,
    )
    print("[gull] Browser closed.")


app = FastAPI(
    title="Gull - Browser Runtime",
    version=GULL_VERSION,
    lifespan=lifespan,
)


@app.post("/exec", response_model=ExecResponse)
async def exec_command(request: ExecRequest) -> ExecResponse:
    """Execute an agent-browser command.

    The command is transparently passed to the agent-browser CLI with
    automatic --session injection for browser context isolation.

    Examples:
        {"cmd": "open https://example.com"}
        {"cmd": "snapshot -i"}
        {"cmd": "click @e1"}
        {"cmd": "fill @e2 'hello world'"}
        {"cmd": "screenshot /workspace/page.png"}
    """
    stdout, stderr, exit_code = await _run_agent_browser(
        request.cmd,
        session=SESSION_NAME,
        profile=BROWSER_PROFILE_DIR,
        timeout=request.timeout,
    )

    return ExecResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint.

    Checks if agent-browser is installed and if a browser session is active.
    """
    # Check if agent-browser is available
    agent_browser_available = shutil.which("agent-browser") is not None

    if not agent_browser_available:
        return HealthResponse(
            status="unhealthy",
            browser_active=False,
            session=SESSION_NAME,
        )

    # Check if our session is active
    stdout, stderr, code = await _run_agent_browser(
        "session list",
        # Do NOT bind to a session here; we want to list all active sessions.
        session=None,
        profile=BROWSER_PROFILE_DIR,
        timeout=5,
    )

    browser_active = SESSION_NAME in stdout if code == 0 else False

    return HealthResponse(
        status="healthy" if agent_browser_available else "unhealthy",
        browser_active=browser_active,
        session=SESSION_NAME,
    )


@app.get("/meta", response_model=MetaResponse)
async def meta() -> MetaResponse:
    """Runtime metadata endpoint.

    Returns capabilities and version information for Bay's CapabilityRouter.
    Format matches Ship's /meta response for consistency.

    Note: screenshot is NOT a separate capability. Use agent-browser's
    `screenshot /workspace/xxx.png` command via browser exec, then download
    via Ship's filesystem capability (both containers share the Cargo Volume).
    """
    return MetaResponse(
        runtime={
            "name": "gull",
            "version": GULL_VERSION,
            "api_version": "v1",
        },
        workspace={
            "mount_path": WORKSPACE_PATH,
        },
        capabilities={
            "browser": {"version": "1.0"},
        },
    )
