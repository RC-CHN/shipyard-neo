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
import logging
import os
import re
import shlex
import shutil
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def get_version() -> str:
    """Get version from pyproject.toml (single source of truth)."""
    try:
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "unknown")
    except Exception:
        return "unknown"


# Configuration from environment
SESSION_NAME = os.environ.get("SANDBOX_ID", os.environ.get("BAY_SANDBOX_ID", "default"))
WORKSPACE_PATH = os.environ.get("BAY_WORKSPACE_PATH", "/workspace")
# Persistent browser profile directory on shared Cargo Volume.
# agent-browser --profile automatically persists cookies, localStorage,
# IndexedDB, service workers, and cache to this directory.
BROWSER_PROFILE_DIR = os.path.join(WORKSPACE_PATH, ".browser", "profile")
GULL_VERSION = get_version()

# Browser readiness state - set to True after successful pre-warming in lifespan.
# Safe for concurrent reads: single write in lifespan, multiple reads in /health.
_browser_ready: bool = False


class ExecRequest(BaseModel):
    """Request to execute an agent-browser command."""

    cmd: str = Field(
        ..., description="agent-browser command (without 'agent-browser' prefix)"
    )
    timeout: int = Field(default=30, description="Timeout in seconds", ge=1, le=300)


class ExecResponse(BaseModel):
    """Response from command execution."""

    stdout: str
    stderr: str
    exit_code: int


class BatchExecRequest(BaseModel):
    """Request to execute a batch of agent-browser commands."""

    commands: list[str] = Field(
        ..., min_length=1, description="List of commands (without agent-browser prefix)"
    )
    timeout: int = Field(
        default=60, ge=1, le=600, description="Overall timeout (seconds)"
    )
    stop_on_error: bool = Field(default=True, description="Stop if a command fails")


class BatchStepResult(BaseModel):
    """Result of a single step in a batch."""

    cmd: str
    stdout: str
    stderr: str
    exit_code: int
    step_index: int
    duration_ms: int


class BatchExecResponse(BaseModel):
    """Response from batch command execution."""

    results: list[BatchStepResult]
    total_steps: int
    completed_steps: int
    success: bool
    duration_ms: int


class HealthResponse(BaseModel):
    """Health check response."""

    status: str  # healthy | degraded | unhealthy
    browser_active: bool
    browser_ready: bool
    session: str
    version: str


class MetaResponse(BaseModel):
    """Runtime metadata response."""

    runtime: dict
    workspace: dict
    capabilities: dict
    built_in_skills: list[dict] = []


async def _run_agent_browser(
    cmd: str,
    *,
    timeout: float = 30.0,
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


# ---------------------------------------------------------------------------
# Built-in skills helpers
# ---------------------------------------------------------------------------

SKILLS_SRC_DIR = Path("/app/skills")


def _scan_built_in_skills(root: Path = SKILLS_SRC_DIR) -> list[dict]:
    """Scan /app/skills/*/SKILL.md, parse YAML frontmatter, return metadata."""
    skills: list[dict] = []
    if not root.exists():
        return skills

    for skill_dir in sorted(root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta = _parse_frontmatter(text)
            skills.append(
                {
                    "name": meta.get("name", skill_dir.name),
                    "description": meta.get("description", ""),
                    "path": str(skill_md),
                }
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to parse %s: %s", skill_md, exc)
    return skills


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from markdown text (simple parser)."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip("'\"")
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager.

    On startup:
    - Ensure browser profile directory exists on Cargo Volume.
    - Pre-warm Chromium browser by opening about:blank.
      This triggers Playwright + Chromium initialization so subsequent
      commands don't incur cold-start latency.
    - agent-browser --profile automatically restores persisted state
      (cookies, localStorage, etc.) on first command.

    On shutdown:
    - Close the browser session. agent-browser --profile automatically
      persists state to the profile directory.

    Note: Built-in skills injection is handled by entrypoint.sh (shell layer),
    not in Python lifespan, for security and consistency with Ship.
    """
    global _browser_ready

    # Ensure profile dir exists on shared Cargo Volume
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    print(f"[gull] Starting Gull v{GULL_VERSION}, session={SESSION_NAME}")
    print(f"[gull] Browser profile dir: {BROWSER_PROFILE_DIR}")

    # Pre-warm browser: trigger Chromium startup via `open about:blank`.
    # Similar to Ship pre-warming Jupyter Kernel to avoid first-request latency.
    # Failure does NOT block service startup (graceful degradation).
    try:
        print("[gull] Pre-warming browser (open about:blank)...")
        stdout, stderr, code = await _run_agent_browser(
            "open about:blank",
            session=SESSION_NAME,
            profile=BROWSER_PROFILE_DIR,
            timeout=30,
        )
        if code == 0:
            _browser_ready = True
            print("[gull] Browser pre-warmed successfully")
        else:
            print(f"[gull] Browser pre-warm returned exit_code={code}: {stderr}")
    except Exception as e:
        # Pre-warm failure is not fatal; first user command will trigger startup
        print(f"[gull] Failed to pre-warm browser: {e}")

    yield

    # Shutdown: close browser (profile auto-persists state)
    print("[gull] Shutting down, closing browser...")
    await _run_agent_browser(
        "close",
        session=SESSION_NAME,
        profile=BROWSER_PROFILE_DIR,
        timeout=5,
    )
    _browser_ready = False
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


@app.post("/exec_batch", response_model=BatchExecResponse)
async def exec_batch(request: BatchExecRequest) -> BatchExecResponse:
    """Execute a batch of agent-browser commands sequentially.

    Loops over commands calling _run_agent_browser() for each.
    Tracks per-step timing and respects overall timeout budget.
    If stop_on_error is True, stops on first non-zero exit code.

    Examples:
        {
            "commands": [
                "open https://example.com",
                "wait --load networkidle",
                "snapshot -i"
            ],
            "timeout": 60,
            "stop_on_error": true
        }
    """
    batch_start = time.perf_counter()
    results: list[BatchStepResult] = []

    for i, cmd in enumerate(request.commands):
        # Calculate remaining timeout budget
        elapsed = time.perf_counter() - batch_start
        remaining_timeout = request.timeout - elapsed
        if remaining_timeout <= 0:
            break

        step_start = time.perf_counter()
        stdout, stderr, exit_code = await _run_agent_browser(
            cmd,
            session=SESSION_NAME,
            profile=BROWSER_PROFILE_DIR,
            timeout=remaining_timeout,
        )
        step_duration_ms = int((time.perf_counter() - step_start) * 1000)

        results.append(
            BatchStepResult(
                cmd=cmd,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                step_index=i,
                duration_ms=step_duration_ms,
            )
        )

        if request.stop_on_error and exit_code != 0:
            break

    total_duration_ms = int((time.perf_counter() - batch_start) * 1000)

    return BatchExecResponse(
        results=results,
        total_steps=len(request.commands),
        completed_steps=len(results),
        success=(
            len(results) == len(request.commands)
            and all(r.exit_code == 0 for r in results)
        ),
        duration_ms=total_duration_ms,
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint.

    Checks if agent-browser is installed, if a browser session is active,
    and whether the browser has been pre-warmed and is ready to accept commands.

    The `browser_ready` field indicates whether Chromium was successfully
    pre-warmed during startup. Bay uses this field in _wait_for_ready()
    to determine when a Gull session is truly operational.

    Status values:
    - "healthy": agent-browser CLI is available and responsive
    - "degraded": agent-browser exists but CLI probe failed
    - "unhealthy": agent-browser binary not found
    """
    # Check if agent-browser is available
    agent_browser_available = shutil.which("agent-browser") is not None

    if not agent_browser_available:
        return HealthResponse(
            status="unhealthy",
            browser_active=False,
            browser_ready=False,
            session=SESSION_NAME,
            version=GULL_VERSION,
        )

    # Check if our session is active
    stdout, stderr, code = await _run_agent_browser(
        "session list",
        # Do NOT bind to a session here; we want to list all active sessions.
        session=None,
        profile=BROWSER_PROFILE_DIR,
        timeout=5,
    )

    probe_ok = code == 0
    browser_active = SESSION_NAME in stdout if probe_ok else False
    status = "healthy" if probe_ok else "degraded"

    return HealthResponse(
        status=status,
        browser_active=browser_active,
        browser_ready=_browser_ready,
        session=SESSION_NAME,
        version=GULL_VERSION,
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
        built_in_skills=_scan_built_in_skills(),
    )
