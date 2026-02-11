"""Shipyard Neo MCP Server implementation.

This server exposes Shipyard Neo SDK functionality through MCP protocol,
allowing AI agents to create sandboxes and execute code securely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

from shipyard_neo import BayClient, BayError


logger = logging.getLogger("shipyard_neo_mcp")


# Global client instance (managed by lifespan)
_client: BayClient | None = None
_sandboxes: OrderedDict[str, Any] = OrderedDict()  # Cache sandbox objects by ID
_sandboxes_lock: asyncio.Lock | None = None  # Initialized lazily


def _get_lock() -> asyncio.Lock:
    """Return the sandbox cache lock, creating it lazily if needed."""
    global _sandboxes_lock
    if _sandboxes_lock is None:
        _sandboxes_lock = asyncio.Lock()
    return _sandboxes_lock


def _read_positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


_MAX_TOOL_TEXT_CHARS = _read_positive_int_env("SHIPYARD_MAX_TOOL_TEXT_CHARS", 12000)
_MAX_SANDBOX_CACHE_SIZE = _read_positive_int_env("SHIPYARD_SANDBOX_CACHE_SIZE", 256)
_MAX_WRITE_FILE_BYTES = _read_positive_int_env(
    "SHIPYARD_MAX_WRITE_FILE_BYTES", 5 * 1024 * 1024
)
_MAX_TRANSFER_FILE_BYTES = _read_positive_int_env(
    "SHIPYARD_MAX_TRANSFER_FILE_BYTES", 50 * 1024 * 1024
)
_SDK_CALL_TIMEOUT = _read_positive_int_env("SHIPYARD_SDK_CALL_TIMEOUT", 600)

# Sandbox ID format: alphanumeric + hyphens + underscores, 1-128 chars
_SANDBOX_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _validate_relative_path(path: str) -> str:
    """Basic local validation for workspace-relative paths.

    Bay will perform authoritative validation, but doing a lightweight check here:
    - improves error messages
    - avoids sending obviously invalid requests
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("field 'path' must be a non-empty string")
    if path.startswith("/"):
        raise ValueError("invalid path: absolute paths are not allowed")
    # Normalize separators a bit (still let Bay do strict validation)
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("invalid path: path traversal ('..') is not allowed")
    return path


def _validate_local_path(local_path: str) -> Path:
    """Validate and resolve a local filesystem path.

    Ensures the path is absolute (or resolves relative to cwd),
    and does not contain null bytes.
    """
    if not isinstance(local_path, str) or not local_path.strip():
        raise ValueError("field 'local_path' must be a non-empty string")
    if "\x00" in local_path:
        raise ValueError("invalid local_path: null bytes not allowed")
    resolved = Path(local_path).expanduser().resolve()
    return resolved


def _truncate_text(text: str | None, *, limit: int = _MAX_TOOL_TEXT_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    trimmed = text[:limit]
    hidden = len(text) - limit
    return f"{trimmed}\n\n...[truncated {hidden} chars; original={len(text)}]"


def _require_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required field: {key}")
    return value


def _validate_sandbox_id(arguments: dict[str, Any]) -> str:
    """Extract and validate sandbox_id format to prevent injection."""
    sandbox_id = _require_str(arguments, "sandbox_id")
    if not _SANDBOX_ID_RE.match(sandbox_id):
        raise ValueError(
            "invalid sandbox_id format: must be 1-128 alphanumeric/hyphen/underscore characters"
        )
    return sandbox_id


def _optional_str(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be a string")
    return value


def _read_bool(arguments: dict[str, Any], key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"field '{key}' must be a boolean")


def _read_int(
    arguments: dict[str, Any],
    key: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field '{key}' must be an integer")
    if min_value is not None and value < min_value:
        raise ValueError(f"field '{key}' must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"field '{key}' must be <= {max_value}")
    return value


def _read_optional_number(arguments: dict[str, Any], key: str) -> float | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"field '{key}' must be a number")
    return float(value)


def _read_exec_type(arguments: dict[str, Any], key: str = "exec_type") -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be a string")
    if value not in {"python", "shell", "browser", "browser_batch"}:
        raise ValueError(
            f"field '{key}' must be one of: python, shell, browser, browser_batch"
        )
    return value


def _read_release_stage(
    arguments: dict[str, Any],
    *,
    key: str = "stage",
    default: str | None = "canary",
    required: bool = False,
) -> str | None:
    if required:
        value = arguments.get(key)
    else:
        value = arguments.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be a string")
    if value not in {"canary", "stable"}:
        raise ValueError(f"field '{key}' must be one of: canary, stable")
    return value


def _require_str_list(arguments: dict[str, Any], key: str) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"field '{key}' must be a non-empty array of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"field '{key}' must be a non-empty array of strings")
        normalized.append(item)
    return normalized


def _cache_sandbox(sandbox: Any) -> None:
    sandbox_id = getattr(sandbox, "id", None)
    if not isinstance(sandbox_id, str) or not sandbox_id:
        return
    if sandbox_id in _sandboxes:
        _sandboxes.move_to_end(sandbox_id)
    _sandboxes[sandbox_id] = sandbox
    while len(_sandboxes) > _MAX_SANDBOX_CACHE_SIZE:
        evicted_id, _ = _sandboxes.popitem(last=False)
        logger.debug(
            "cache_evict sandbox_id=%s cache_size=%d", evicted_id, len(_sandboxes)
        )


def _format_bay_error(error: BayError) -> str:
    suffix = ""
    if error.details:
        serialized = json.dumps(error.details, ensure_ascii=False, default=str)
        suffix = f"\n\ndetails: {_truncate_text(serialized, limit=1000)}"
    return f"**API Error:** [{error.code}] {error.message}{suffix}"


def get_config() -> dict[str, Any]:
    """Get configuration from environment variables."""
    endpoint = os.environ.get("SHIPYARD_ENDPOINT_URL") or os.environ.get("BAY_ENDPOINT")
    token = os.environ.get("SHIPYARD_ACCESS_TOKEN") or os.environ.get("BAY_TOKEN")

    if not endpoint:
        raise ValueError(
            "SHIPYARD_ENDPOINT_URL environment variable is required. "
            "Set it in your MCP configuration."
        )
    if not token:
        raise ValueError(
            "SHIPYARD_ACCESS_TOKEN environment variable is required. "
            "Set it in your MCP configuration."
        )

    default_profile = os.environ.get("SHIPYARD_DEFAULT_PROFILE", "python-default")

    # Allow ttl=0 for infinite TTL.
    default_ttl_raw = os.environ.get("SHIPYARD_DEFAULT_TTL", "3600")
    try:
        default_ttl = int(default_ttl_raw)
    except ValueError:
        default_ttl = 3600
    if default_ttl < 0:
        default_ttl = 3600

    return {
        "endpoint_url": endpoint,
        "access_token": token,
        "default_profile": default_profile,
        "default_ttl": default_ttl,
    }


@asynccontextmanager
async def lifespan(server: Server):
    """Manage the BayClient lifecycle."""
    global _client
    config = get_config()

    _client = BayClient(
        endpoint_url=config["endpoint_url"],
        access_token=config["access_token"],
    )
    await _client.__aenter__()

    try:
        yield
    finally:
        await _client.__aexit__(None, None, None)
        _client = None
        _sandboxes.clear()


# Create MCP server
server = Server("shipyard-neo-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="create_sandbox",
            description="Create a new sandbox environment for executing code. Returns the sandbox ID which must be used for subsequent operations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Runtime profile (e.g., 'python-default'). Defaults to 'python-default'.",
                    },
                    "ttl": {
                        "type": "integer",
                        "description": "Time-to-live in seconds. Defaults to 3600 (1 hour). Use 0 for no expiration.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="delete_sandbox",
            description="Delete a sandbox and clean up all resources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID to delete.",
                    },
                },
                "required": ["sandbox_id"],
            },
        ),
        Tool(
            name="execute_python",
            description="Execute Python code in a sandbox. Variables persist across calls within the same sandbox session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID to execute in.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Python code to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds. Defaults to 30.",
                    },
                    "include_code": {
                        "type": "boolean",
                        "description": "Include executed code and execution metadata in response.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description for execution history annotation.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags for execution history.",
                    },
                },
                "required": ["sandbox_id", "code"],
            },
        ),
        Tool(
            name="execute_shell",
            description="Execute a shell command in a sandbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID to execute in.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (relative to /workspace). Defaults to workspace root.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds. Defaults to 30.",
                    },
                    "include_code": {
                        "type": "boolean",
                        "description": "Include executed command and execution metadata in response.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description for execution history annotation.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags for execution history.",
                    },
                },
                "required": ["sandbox_id", "command"],
            },
        ),
        Tool(
            name="read_file",
            description="Read a file from the sandbox workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path relative to /workspace.",
                    },
                },
                "required": ["sandbox_id", "path"],
            },
        ),
        Tool(
            name="write_file",
            description="Write content to a file in the sandbox workspace. Creates parent directories automatically.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path relative to /workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write.",
                    },
                },
                "required": ["sandbox_id", "path", "content"],
            },
        ),
        Tool(
            name="list_files",
            description="List files and directories in the sandbox workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to /workspace. Defaults to '.' (workspace root).",
                    },
                },
                "required": ["sandbox_id"],
            },
        ),
        Tool(
            name="delete_file",
            description="Delete a file or directory from the sandbox workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Path to delete, relative to /workspace.",
                    },
                },
                "required": ["sandbox_id", "path"],
            },
        ),
        Tool(
            name="get_execution_history",
            description="Get execution history for a sandbox with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {"type": "string", "description": "The sandbox ID."},
                    "exec_type": {
                        "type": "string",
                        "description": "Optional execution type filter: python / shell / browser / browser_batch.",
                    },
                    "success_only": {
                        "type": "boolean",
                        "description": "Return only successful executions.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of entries. Defaults to 50.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags filter.",
                    },
                    "has_notes": {
                        "type": "boolean",
                        "description": "Return only entries that have notes.",
                    },
                    "has_description": {
                        "type": "boolean",
                        "description": "Return only entries that have description.",
                    },
                },
                "required": ["sandbox_id"],
            },
        ),
        Tool(
            name="get_execution",
            description="Get one execution record by execution ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {"type": "string", "description": "The sandbox ID."},
                    "execution_id": {
                        "type": "string",
                        "description": "Execution record ID.",
                    },
                },
                "required": ["sandbox_id", "execution_id"],
            },
        ),
        Tool(
            name="get_last_execution",
            description="Get the latest execution record in a sandbox.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {"type": "string", "description": "The sandbox ID."},
                    "exec_type": {
                        "type": "string",
                        "description": "Optional execution type filter: python / shell / browser / browser_batch.",
                    },
                },
                "required": ["sandbox_id"],
            },
        ),
        Tool(
            name="annotate_execution",
            description="Add or update description/tags/notes for one execution record.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {"type": "string", "description": "The sandbox ID."},
                    "execution_id": {
                        "type": "string",
                        "description": "Execution record ID.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Description text.",
                    },
                    "tags": {"type": "string", "description": "Comma-separated tags."},
                    "notes": {"type": "string", "description": "Agent notes."},
                },
                "required": ["sandbox_id", "execution_id"],
            },
        ),
        Tool(
            name="create_skill_candidate",
            description="Create a reusable skill candidate from execution IDs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_key": {"type": "string", "description": "Skill identifier."},
                    "source_execution_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Execution IDs used as source evidence.",
                    },
                    "scenario_key": {
                        "type": "string",
                        "description": "Optional scenario key.",
                    },
                    "payload_ref": {
                        "type": "string",
                        "description": "Optional payload reference.",
                    },
                },
                "required": ["skill_key", "source_execution_ids"],
            },
        ),
        Tool(
            name="evaluate_skill_candidate",
            description="Record evaluation result for a skill candidate.",
            inputSchema={
                "type": "object",
                "properties": {
                    "candidate_id": {
                        "type": "string",
                        "description": "Skill candidate ID.",
                    },
                    "passed": {
                        "type": "boolean",
                        "description": "Whether evaluation passed.",
                    },
                    "score": {
                        "type": "number",
                        "description": "Optional evaluation score.",
                    },
                    "benchmark_id": {
                        "type": "string",
                        "description": "Optional benchmark ID.",
                    },
                    "report": {
                        "type": "string",
                        "description": "Optional evaluation report.",
                    },
                },
                "required": ["candidate_id", "passed"],
            },
        ),
        Tool(
            name="promote_skill_candidate",
            description="Promote a passing skill candidate to release.",
            inputSchema={
                "type": "object",
                "properties": {
                    "candidate_id": {
                        "type": "string",
                        "description": "Skill candidate ID.",
                    },
                    "stage": {
                        "type": "string",
                        "description": "Release stage: canary or stable. Defaults to canary.",
                    },
                },
                "required": ["candidate_id"],
            },
        ),
        Tool(
            name="list_skill_candidates",
            description="List skill candidates with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional status filter.",
                    },
                    "skill_key": {
                        "type": "string",
                        "description": "Optional skill key filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items. Defaults to 50.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset. Defaults to 0.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="list_skill_releases",
            description="List skill releases with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_key": {
                        "type": "string",
                        "description": "Optional skill key filter.",
                    },
                    "active_only": {
                        "type": "boolean",
                        "description": "Only active releases.",
                    },
                    "stage": {
                        "type": "string",
                        "description": "Optional stage filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items. Defaults to 50.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset. Defaults to 0.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="rollback_skill_release",
            description="Rollback an active release to a previous known-good version.",
            inputSchema={
                "type": "object",
                "properties": {
                    "release_id": {
                        "type": "string",
                        "description": "Release ID to rollback from.",
                    },
                },
                "required": ["release_id"],
            },
        ),
        Tool(
            name="execute_browser",
            description=(
                "Execute a browser automation command in a sandbox. "
                "The command should NOT include the 'agent-browser' prefix — it is injected automatically. "
                "Examples: 'open https://example.com', 'snapshot -i', 'click @e1', 'fill @e2 \"text\"'. "
                "The sandbox must have browser capability (use a browser-enabled profile)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID to execute in.",
                    },
                    "cmd": {
                        "type": "string",
                        "description": (
                            "Browser automation command without 'agent-browser' prefix. "
                            "E.g., 'open https://example.com', 'snapshot -i', 'click @e1'."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds. Defaults to 30.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional execution description for learning evidence.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags for execution evidence.",
                    },
                    "learn": {
                        "type": "boolean",
                        "description": "Whether this execution should enter browser learning pipeline.",
                    },
                    "include_trace": {
                        "type": "boolean",
                        "description": "Persist and return trace_ref for step-level replay trace.",
                    },
                },
                "required": ["sandbox_id", "cmd"],
            },
        ),
        Tool(
            name="execute_browser_batch",
            description=(
                "Execute a sequence of browser automation commands in order within one request. "
                "Use this for deterministic sequences that don't need intermediate reasoning "
                "(e.g., open → fill → click → wait). For flows that need intermediate decisions "
                "(e.g., snapshot → analyze → decide), use individual execute_browser calls instead. "
                "Commands should NOT include the 'agent-browser' prefix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID to execute in.",
                    },
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of browser commands without 'agent-browser' prefix. "
                            "E.g., ['open https://example.com', 'wait --load networkidle', 'snapshot -i']."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Overall timeout in seconds for all commands. Defaults to 60.",
                    },
                    "stop_on_error": {
                        "type": "boolean",
                        "description": "Stop execution if a command fails. Defaults to true.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional execution description for learning evidence.",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Optional comma-separated tags for execution evidence.",
                    },
                    "learn": {
                        "type": "boolean",
                        "description": "Whether this execution should enter browser learning pipeline.",
                    },
                    "include_trace": {
                        "type": "boolean",
                        "description": "Persist and return trace_ref for step-level replay trace.",
                    },
                },
                "required": ["sandbox_id", "commands"],
            },
        ),
        Tool(
            name="upload_file",
            description=(
                "Upload a local file to a sandbox workspace. "
                "Reads a file from the local filesystem (where the MCP server runs) "
                "and uploads it to the sandbox. Supports binary files (images, PDFs, "
                "archives, etc.). Use this instead of write_file when dealing with "
                "binary content or existing local files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID.",
                    },
                    "local_path": {
                        "type": "string",
                        "description": (
                            "Absolute or relative path to the local file to upload. "
                            "Relative paths are resolved from the MCP server's working directory."
                        ),
                    },
                    "sandbox_path": {
                        "type": "string",
                        "description": (
                            "Target path in the sandbox workspace, relative to /workspace. "
                            "If not provided, uses the local file's name."
                        ),
                    },
                },
                "required": ["sandbox_id", "local_path"],
            },
        ),
        Tool(
            name="download_file",
            description=(
                "Download a file from a sandbox workspace to the local filesystem. "
                "Fetches a file from the sandbox and saves it locally (where the MCP "
                "server runs). Supports binary files (images, PDFs, archives, etc.). "
                "Use this instead of read_file when you need the actual file on disk "
                "or when dealing with binary content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sandbox_id": {
                        "type": "string",
                        "description": "The sandbox ID.",
                    },
                    "sandbox_path": {
                        "type": "string",
                        "description": "File path in the sandbox, relative to /workspace.",
                    },
                    "local_path": {
                        "type": "string",
                        "description": (
                            "Local destination path. Absolute or relative to the MCP server's "
                            "working directory. Parent directories will be created if needed. "
                            "If not provided, saves to the current directory using the sandbox file's name."
                        ),
                    },
                },
                "required": ["sandbox_id", "sandbox_path"],
            },
        ),
        Tool(
            name="list_profiles",
            description=(
                "List available sandbox profiles. "
                "Profiles define runtime capabilities (python, shell, filesystem, browser), "
                "resource limits, and idle timeout. Use this to discover which profiles "
                "are available before creating a sandbox."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


async def get_sandbox(sandbox_id: str):
    """Get or fetch a sandbox by ID with cache lock protection."""
    global _client, _sandboxes

    if _client is None:
        raise RuntimeError("BayClient not initialized")

    lock = _get_lock()
    async with lock:
        if sandbox_id in _sandboxes:
            _sandboxes.move_to_end(sandbox_id)
            return _sandboxes[sandbox_id]

    # Fetch from server (outside lock to avoid holding it during I/O)
    async with asyncio.timeout(_SDK_CALL_TIMEOUT):
        sandbox = await _client.get_sandbox(sandbox_id)

    async with lock:
        _cache_sandbox(sandbox)
    return sandbox


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    global _client, _sandboxes

    if _client is None:
        return [TextContent(type="text", text="Error: BayClient not initialized")]

    try:
        if name == "create_sandbox":
            config = get_config()
            profile = arguments.get("profile", config["default_profile"])
            if not isinstance(profile, str) or not profile.strip():
                raise ValueError("field 'profile' must be a non-empty string")
            ttl = _read_int(arguments, "ttl", config["default_ttl"], min_value=0)

            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                sandbox = await _client.create_sandbox(profile=profile, ttl=ttl)
            async with _get_lock():
                _cache_sandbox(sandbox)

            logger.info(
                "sandbox_created sandbox_id=%s profile=%s ttl=%d",
                sandbox.id,
                sandbox.profile,
                ttl,
            )

            # Build containers info if available
            containers_text = ""
            if getattr(sandbox, "containers", None):
                lines = ["**Containers:**"]
                for c in sandbox.containers:
                    ver = getattr(c, "version", None) or "unknown"
                    healthy = getattr(c, "healthy", None)
                    health_str = (
                        "✅" if healthy is True
                        else "❌" if healthy is False
                        else "?"
                    )
                    rt = getattr(c, "runtime_type", "unknown")
                    name = getattr(c, "name", "unknown")
                    caps = ", ".join(getattr(c, "capabilities", []))
                    lines.append(
                        f"  - {name} ({rt}) v{ver} {health_str} [{caps}]"
                    )
                containers_text = "\n".join(lines) + "\n"

            return [
                TextContent(
                    type="text",
                    text=f"Sandbox created successfully.\n\n"
                    f"**Sandbox ID:** `{sandbox.id}`\n"
                    f"**Profile:** {sandbox.profile}\n"
                    f"**Status:** {sandbox.status.value}\n"
                    f"**Capabilities:** {', '.join(sandbox.capabilities)}\n"
                    f"**TTL:** {ttl} seconds\n"
                    f"{containers_text}\n"
                    f"Use this sandbox_id for subsequent operations.",
                )
            ]

        elif name == "delete_sandbox":
            sandbox_id = _validate_sandbox_id(arguments)
            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                await sandbox.delete()
            async with _get_lock():
                _sandboxes.pop(sandbox_id, None)

            logger.info("sandbox_deleted sandbox_id=%s", sandbox_id)

            return [
                TextContent(
                    type="text",
                    text=f"Sandbox `{sandbox_id}` deleted successfully.",
                )
            ]

        elif name == "execute_python":
            sandbox_id = _validate_sandbox_id(arguments)
            code = _require_str(arguments, "code")
            timeout = _read_int(arguments, "timeout", 30, min_value=1, max_value=300)
            include_code = _read_bool(arguments, "include_code", False)
            description = _optional_str(arguments, "description")
            tags = _optional_str(arguments, "tags")

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                result = await sandbox.python.exec(
                    code,
                    timeout=timeout,
                    include_code=include_code,
                    description=description,
                    tags=tags,
                )

            if result.success:
                output = _truncate_text(result.output or "(no output)")
                suffix = ""
                if result.execution_id:
                    suffix += f"\n\nexecution_id: {result.execution_id}"
                if result.execution_time_ms is not None:
                    suffix += f"\nexecution_time_ms: {result.execution_time_ms}"
                if include_code and result.code:
                    suffix += f"\n\ncode:\n{_truncate_text(result.code)}"
                return [
                    TextContent(
                        type="text",
                        text=f"**Execution successful**\n\n```\n{output}\n```{suffix}",
                    )
                ]
            else:
                error = _truncate_text(result.error or "Unknown error")
                suffix = ""
                if result.execution_id:
                    suffix += f"\n\nexecution_id: {result.execution_id}"
                return [
                    TextContent(
                        type="text",
                        text=f"**Execution failed**\n\n```\n{error}\n```{suffix}",
                    )
                ]

        elif name == "execute_shell":
            sandbox_id = _validate_sandbox_id(arguments)
            command = _require_str(arguments, "command")
            cwd = _optional_str(arguments, "cwd")
            timeout = _read_int(arguments, "timeout", 30, min_value=1, max_value=300)
            include_code = _read_bool(arguments, "include_code", False)
            description = _optional_str(arguments, "description")
            tags = _optional_str(arguments, "tags")

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                result = await sandbox.shell.exec(
                    command,
                    cwd=cwd,
                    timeout=timeout,
                    include_code=include_code,
                    description=description,
                    tags=tags,
                )

            output = _truncate_text(result.output or "(no output)")
            status = "successful" if result.success else "failed"
            exit_code = result.exit_code if result.exit_code is not None else "N/A"
            suffix = ""
            if result.execution_id:
                suffix += f"\n\nexecution_id: {result.execution_id}"
            if result.execution_time_ms is not None:
                suffix += f"\nexecution_time_ms: {result.execution_time_ms}"
            if include_code and result.command:
                suffix += f"\n\ncommand:\n{_truncate_text(result.command)}"

            return [
                TextContent(
                    type="text",
                    text=f"**Command {status}** (exit code: {exit_code})\n\n```\n{output}\n```{suffix}",
                )
            ]

        elif name == "read_file":
            sandbox_id = _validate_sandbox_id(arguments)
            path = _validate_relative_path(_require_str(arguments, "path"))

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                raw = await sandbox.filesystem.read_file(path)
            content = _truncate_text(raw)

            return [
                TextContent(
                    type="text",
                    text=f"**File: {path}**\n\n```\n{content}\n```",
                )
            ]

        elif name == "write_file":
            sandbox_id = _validate_sandbox_id(arguments)
            path = _validate_relative_path(_require_str(arguments, "path"))
            content = _require_str(arguments, "content")

            content_bytes = len(content.encode("utf-8"))
            if content_bytes > _MAX_WRITE_FILE_BYTES:
                raise ValueError(
                    f"write_file content too large: {content_bytes} bytes "
                    f"exceeds limit of {_MAX_WRITE_FILE_BYTES} bytes"
                )

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                await sandbox.filesystem.write_file(path, content)

            return [
                TextContent(
                    type="text",
                    text=f"File `{path}` written successfully ({len(content)} bytes).",
                )
            ]

        elif name == "list_files":
            sandbox_id = _validate_sandbox_id(arguments)
            path = arguments.get("path", ".")
            if not isinstance(path, str):
                raise ValueError("field 'path' must be a string")
            path = _validate_relative_path(path)

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                entries = await sandbox.filesystem.list_dir(path)

            if not entries:
                return [
                    TextContent(
                        type="text",
                        text=f"Directory `{path}` is empty.",
                    )
                ]

            lines = [f"**Directory: {path}**\n"]
            for entry in entries:
                if entry.is_dir:
                    lines.append(f"📁 {entry.name}/")
                else:
                    size = f" ({entry.size} bytes)" if entry.size is not None else ""
                    lines.append(f"📄 {entry.name}{size}")

            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "delete_file":
            sandbox_id = _validate_sandbox_id(arguments)
            path = _validate_relative_path(_require_str(arguments, "path"))

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                await sandbox.filesystem.delete(path)

            return [
                TextContent(
                    type="text",
                    text=f"Deleted `{path}` successfully.",
                )
            ]

        elif name == "get_execution_history":
            sandbox_id = _validate_sandbox_id(arguments)
            sandbox = await get_sandbox(sandbox_id)

            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                history = await sandbox.get_execution_history(
                    exec_type=_read_exec_type(arguments, "exec_type"),
                    success_only=_read_bool(arguments, "success_only", False),
                    limit=_read_int(arguments, "limit", 50, min_value=1, max_value=500),
                    tags=_optional_str(arguments, "tags"),
                    has_notes=_read_bool(arguments, "has_notes", False),
                    has_description=_read_bool(arguments, "has_description", False),
                )

            if not history.entries:
                return [TextContent(type="text", text="No execution history found.")]

            lines = [f"Total: {history.total}"]
            for entry in history.entries:
                lines.append(
                    f"- {entry.id} | {entry.exec_type} | success={entry.success} | {entry.execution_time_ms}ms"
                )
                if entry.description:
                    lines.append(f"  description: {entry.description}")
                if entry.tags:
                    lines.append(f"  tags: {entry.tags}")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_execution":
            sandbox_id = _validate_sandbox_id(arguments)
            execution_id = _require_str(arguments, "execution_id")
            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                entry = await sandbox.get_execution(execution_id)
            return [
                TextContent(
                    type="text",
                    text=(
                        f"execution_id: {entry.id}\n"
                        f"type: {entry.exec_type}\n"
                        f"success: {entry.success}\n"
                        f"time_ms: {entry.execution_time_ms}\n"
                        f"tags: {entry.tags or ''}\n"
                        f"description: {entry.description or ''}\n"
                        f"notes: {entry.notes or ''}\n\n"
                        f"code:\n{_truncate_text(entry.code)}\n\n"
                        f"output:\n{_truncate_text(entry.output)}\n\n"
                        f"error:\n{_truncate_text(entry.error)}"
                    ),
                )
            ]

        elif name == "get_last_execution":
            sandbox_id = _validate_sandbox_id(arguments)
            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                entry = await sandbox.get_last_execution(
                    exec_type=_read_exec_type(arguments, "exec_type")
                )
            return [
                TextContent(
                    type="text",
                    text=(
                        f"execution_id: {entry.id}\n"
                        f"type: {entry.exec_type}\n"
                        f"success: {entry.success}\n"
                        f"time_ms: {entry.execution_time_ms}\n"
                        f"code:\n{_truncate_text(entry.code)}"
                    ),
                )
            ]

        elif name == "annotate_execution":
            sandbox_id = _validate_sandbox_id(arguments)
            execution_id = _require_str(arguments, "execution_id")
            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                entry = await sandbox.annotate_execution(
                    execution_id,
                    description=_optional_str(arguments, "description"),
                    tags=_optional_str(arguments, "tags"),
                    notes=_optional_str(arguments, "notes"),
                )
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Updated execution {entry.id}\n"
                        f"description: {entry.description or ''}\n"
                        f"tags: {entry.tags or ''}\n"
                        f"notes: {entry.notes or ''}"
                    ),
                )
            ]

        elif name == "create_skill_candidate":
            skill_key = _require_str(arguments, "skill_key")
            source_execution_ids = _require_str_list(arguments, "source_execution_ids")
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                candidate = await _client.skills.create_candidate(
                    skill_key=skill_key,
                    source_execution_ids=source_execution_ids,
                    scenario_key=_optional_str(arguments, "scenario_key"),
                    payload_ref=_optional_str(arguments, "payload_ref"),
                )
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Created skill candidate {candidate.id}\n"
                        f"skill_key: {candidate.skill_key}\n"
                        f"status: {candidate.status.value}\n"
                        f"source_execution_ids: {', '.join(candidate.source_execution_ids)}"
                    ),
                )
            ]

        elif name == "evaluate_skill_candidate":
            candidate_id = _require_str(arguments, "candidate_id")
            passed = arguments.get("passed")
            if not isinstance(passed, bool):
                raise ValueError("field 'passed' must be a boolean")
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                evaluation = await _client.skills.evaluate_candidate(
                    candidate_id,
                    passed=passed,
                    score=_read_optional_number(arguments, "score"),
                    benchmark_id=_optional_str(arguments, "benchmark_id"),
                    report=_optional_str(arguments, "report"),
                )
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Evaluation recorded: {evaluation.id}\n"
                        f"candidate_id: {evaluation.candidate_id}\n"
                        f"passed: {evaluation.passed}\n"
                        f"score: {evaluation.score}"
                    ),
                )
            ]

        elif name == "promote_skill_candidate":
            candidate_id = _require_str(arguments, "candidate_id")
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                release = await _client.skills.promote_candidate(
                    candidate_id,
                    stage=_read_release_stage(arguments, key="stage", default="canary"),
                )
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Candidate promoted: {candidate_id}\n"
                        f"release_id: {release.id}\n"
                        f"skill_key: {release.skill_key}\n"
                        f"version: {release.version}\n"
                        f"stage: {release.stage.value}\n"
                        f"active: {release.is_active}"
                    ),
                )
            ]

        elif name == "list_skill_candidates":
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                candidates = await _client.skills.list_candidates(
                    status=_optional_str(arguments, "status"),
                    skill_key=_optional_str(arguments, "skill_key"),
                    limit=_read_int(arguments, "limit", 50, min_value=1, max_value=500),
                    offset=_read_int(arguments, "offset", 0, min_value=0),
                )
            if not candidates.items:
                return [TextContent(type="text", text="No skill candidates found.")]
            lines = [f"Total: {candidates.total}"]
            for item in candidates.items:
                lines.append(
                    f"- {item.id} | {item.skill_key} | status={item.status.value} | pass={item.latest_pass}"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "list_skill_releases":
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                releases = await _client.skills.list_releases(
                    skill_key=_optional_str(arguments, "skill_key"),
                    active_only=_read_bool(arguments, "active_only", False),
                    stage=_read_release_stage(
                        arguments, key="stage", required=False, default=None
                    ),
                    limit=_read_int(arguments, "limit", 50, min_value=1, max_value=500),
                    offset=_read_int(arguments, "offset", 0, min_value=0),
                )
            if not releases.items:
                return [TextContent(type="text", text="No skill releases found.")]
            lines = [f"Total: {releases.total}"]
            for item in releases.items:
                lines.append(
                    f"- {item.id} | {item.skill_key} v{item.version} | stage={item.stage.value} | active={item.is_active}"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "rollback_skill_release":
            release_id = _require_str(arguments, "release_id")
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                rollback_release = await _client.skills.rollback_release(release_id)
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Rollback completed.\n"
                        f"new_release_id: {rollback_release.id}\n"
                        f"skill_key: {rollback_release.skill_key}\n"
                        f"version: {rollback_release.version}\n"
                        f"rollback_of: {rollback_release.rollback_of}"
                    ),
                )
            ]

        elif name == "execute_browser":
            sandbox_id = _validate_sandbox_id(arguments)
            cmd = _require_str(arguments, "cmd")
            timeout = _read_int(arguments, "timeout", 30, min_value=1, max_value=300)
            description = _optional_str(arguments, "description")
            tags = _optional_str(arguments, "tags")
            learn = _read_bool(arguments, "learn", False)
            include_trace = _read_bool(arguments, "include_trace", False)

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                result = await sandbox.browser.exec(
                    cmd,
                    timeout=timeout,
                    description=description,
                    tags=tags,
                    learn=learn,
                    include_trace=include_trace,
                )

            output = _truncate_text(result.output or "(no output)")
            status = "successful" if result.success else "failed"
            exit_code = result.exit_code if result.exit_code is not None else "N/A"
            suffix = ""
            execution_id = getattr(result, "execution_id", None)
            execution_time_ms = getattr(result, "execution_time_ms", None)
            trace_ref = getattr(result, "trace_ref", None)
            if execution_id:
                suffix += f"\n\nexecution_id: {execution_id}"
            if execution_time_ms is not None:
                suffix += f"\nexecution_time_ms: {execution_time_ms}"
            if trace_ref:
                suffix += f"\ntrace_ref: {trace_ref}"
            error_suffix = ""
            if not result.success and result.error:
                error_suffix = f"\n\nstderr:\n{_truncate_text(result.error)}"

            return [
                TextContent(
                    type="text",
                    text=(
                        f"**Browser command {status}** (exit code: {exit_code})\n\n"
                        f"```\n{output}\n```{suffix}{error_suffix}"
                    ),
                )
            ]

        elif name == "execute_browser_batch":
            sandbox_id = _validate_sandbox_id(arguments)
            commands = _require_str_list(arguments, "commands")
            timeout = _read_int(arguments, "timeout", 60, min_value=1, max_value=600)
            stop_on_error = _read_bool(arguments, "stop_on_error", True)
            description = _optional_str(arguments, "description")
            tags = _optional_str(arguments, "tags")
            learn = _read_bool(arguments, "learn", False)
            include_trace = _read_bool(arguments, "include_trace", False)

            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                result = await sandbox.browser.exec_batch(
                    commands,
                    timeout=timeout,
                    stop_on_error=stop_on_error,
                    description=description,
                    tags=tags,
                    learn=learn,
                    include_trace=include_trace,
                )

            lines = [
                f"**Batch execution {'completed' if result.success else 'failed'}** "
                f"({result.completed_steps}/{result.total_steps} steps, {result.duration_ms}ms)\n"
            ]
            execution_id = getattr(result, "execution_id", None)
            execution_time_ms = getattr(result, "execution_time_ms", None)
            trace_ref = getattr(result, "trace_ref", None)
            if execution_id:
                lines.append(f"execution_id: {execution_id}")
            if execution_time_ms is not None:
                lines.append(f"execution_time_ms: {execution_time_ms}")
            if trace_ref:
                lines.append(f"trace_ref: {trace_ref}")
            for step in result.results:
                status_icon = "✅" if step.exit_code == 0 else "❌"
                lines.append(
                    f"{status_icon} Step {step.step_index}: `{step.cmd}` "
                    f"(exit={step.exit_code}, {step.duration_ms}ms)"
                )
                if step.stdout.strip():
                    lines.append(
                        f"   stdout: {_truncate_text(step.stdout.strip(), limit=500)}"
                    )
                if step.stderr.strip():
                    lines.append(
                        f"   stderr: {_truncate_text(step.stderr.strip(), limit=500)}"
                    )

            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "upload_file":
            sandbox_id = _validate_sandbox_id(arguments)
            local_path_str = _require_str(arguments, "local_path")
            local_path = _validate_local_path(local_path_str)

            # Determine sandbox target path
            sandbox_path_raw = _optional_str(arguments, "sandbox_path")
            if sandbox_path_raw:
                sandbox_path = _validate_relative_path(sandbox_path_raw)
            else:
                sandbox_path = local_path.name

            # Validate local file exists and is readable
            if not local_path.exists():
                raise ValueError(f"local file not found: {local_path}")
            if not local_path.is_file():
                raise ValueError(f"local path is not a file: {local_path}")

            # Check file size
            file_size = local_path.stat().st_size
            if file_size > _MAX_TRANSFER_FILE_BYTES:
                raise ValueError(
                    f"file too large: {file_size} bytes "
                    f"exceeds limit of {_MAX_TRANSFER_FILE_BYTES} bytes"
                )

            # Read and upload
            content = local_path.read_bytes()
            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                await sandbox.filesystem.upload(sandbox_path, content)

            logger.info(
                "file_uploaded sandbox_id=%s local=%s sandbox=%s size=%d",
                sandbox_id,
                local_path,
                sandbox_path,
                file_size,
            )

            return [
                TextContent(
                    type="text",
                    text=(
                        f"File uploaded successfully.\n\n"
                        f"**Local:** `{local_path}`\n"
                        f"**Sandbox:** `{sandbox_path}`\n"
                        f"**Size:** {file_size} bytes"
                    ),
                )
            ]

        elif name == "download_file":
            sandbox_id = _validate_sandbox_id(arguments)
            sandbox_path = _validate_relative_path(
                _require_str(arguments, "sandbox_path")
            )

            # Determine local destination
            local_path_str = _optional_str(arguments, "local_path")
            if local_path_str:
                local_path = _validate_local_path(local_path_str)
            else:
                # Use sandbox file name in current directory
                local_path = Path.cwd() / Path(sandbox_path).name

            # Download from sandbox
            sandbox = await get_sandbox(sandbox_id)
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                content = await sandbox.filesystem.download(sandbox_path)

            # Check downloaded size
            if len(content) > _MAX_TRANSFER_FILE_BYTES:
                raise ValueError(
                    f"downloaded file too large: {len(content)} bytes "
                    f"exceeds limit of {_MAX_TRANSFER_FILE_BYTES} bytes"
                )

            # Create parent directories and write
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(content)

            logger.info(
                "file_downloaded sandbox_id=%s sandbox=%s local=%s size=%d",
                sandbox_id,
                sandbox_path,
                local_path,
                len(content),
            )

            return [
                TextContent(
                    type="text",
                    text=(
                        f"File downloaded successfully.\n\n"
                        f"**Sandbox:** `{sandbox_path}`\n"
                        f"**Local:** `{local_path}`\n"
                        f"**Size:** {len(content)} bytes"
                    ),
                )
            ]

        elif name == "list_profiles":
            async with asyncio.timeout(_SDK_CALL_TIMEOUT):
                profiles = await _client.list_profiles(detail=True)

            if not profiles.items:
                return [TextContent(type="text", text="No profiles available.")]

            lines = [f"**Available Profiles** ({len(profiles.items)})\n"]
            for p in profiles.items:
                caps = ", ".join(p.capabilities) if p.capabilities else "none"
                desc = f" — {p.description}" if p.description else ""
                lines.append(
                    f"- **{p.id}**{desc}: capabilities=[{caps}], "
                    f"idle_timeout={p.idle_timeout}s"
                )
                if p.containers:
                    for c in p.containers:
                        c_caps = ", ".join(c.capabilities) if c.capabilities else "none"
                        lines.append(f"    └ {c.name} ({c.runtime_type}): [{c_caps}]")

            return [TextContent(type="text", text="\n".join(lines))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except ValueError as e:
        return [TextContent(type="text", text=f"**Validation Error:** {e!s}")]
    except TimeoutError:
        logger.warning("tool_timeout tool=%s timeout=%ds", name, _SDK_CALL_TIMEOUT)
        return [
            TextContent(
                type="text",
                text=f"**Timeout Error:** SDK call timed out after {_SDK_CALL_TIMEOUT}s",
            )
        ]
    except BayError as e:
        logger.warning("bay_error tool=%s code=%s message=%s", name, e.code, e.message)
        return [TextContent(type="text", text=_format_bay_error(e))]
    except Exception as e:
        logger.exception("unexpected_error tool=%s", name)
        return [TextContent(type="text", text=f"**Error:** {e!s}")]


async def run_server():
    """Run the MCP server."""
    async with lifespan(server):
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )


def main():
    """Main entry point."""
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Server error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
