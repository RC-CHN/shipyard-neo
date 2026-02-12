"""MCP tool schema definitions."""

from __future__ import annotations

from mcp.types import Tool


def get_tool_definitions() -> list[Tool]:
    """Return all MCP tool definitions with their JSON schemas."""
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
            name="create_skill_payload",
            description="Create a generic skill payload and return a stable payload_ref.",
            inputSchema={
                "type": "object",
                "properties": {
                    "payload": {
                        "anyOf": [
                            {"type": "object"},
                            {"type": "array", "items": {}},
                        ],
                        "description": "JSON object/array payload content.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Optional payload kind. Defaults to generic.",
                    },
                },
                "required": ["payload"],
            },
        ),
        Tool(
            name="get_skill_payload",
            description="Get one skill payload by payload_ref.",
            inputSchema={
                "type": "object",
                "properties": {
                    "payload_ref": {
                        "type": "string",
                        "description": "Payload reference (e.g., blob:blob-xxx).",
                    },
                },
                "required": ["payload_ref"],
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
