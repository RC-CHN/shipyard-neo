"""Parameter validation and parsing utilities for MCP tool arguments."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# Sandbox ID format: alphanumeric + hyphens + underscores, 1-128 chars
_SANDBOX_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def validate_relative_path(path: str) -> str:
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


def validate_local_path(local_path: str) -> Path:
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


def truncate_text(text: str | None, *, limit: int) -> str:
    """Truncate text to a maximum length with a trailing indicator."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    trimmed = text[:limit]
    hidden = len(text) - limit
    return f"{trimmed}\n\n...[truncated {hidden} chars; original={len(text)}]"


def require_str(arguments: dict[str, Any], key: str) -> str:
    """Extract a required non-empty string from arguments."""
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required field: {key}")
    return value


def validate_sandbox_id(arguments: dict[str, Any]) -> str:
    """Extract and validate sandbox_id format to prevent injection."""
    sandbox_id = require_str(arguments, "sandbox_id")
    if not _SANDBOX_ID_RE.match(sandbox_id):
        raise ValueError(
            "invalid sandbox_id format: must be 1-128 alphanumeric/hyphen/underscore characters"
        )
    return sandbox_id


def optional_str(arguments: dict[str, Any], key: str) -> str | None:
    """Extract an optional string from arguments."""
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be a string")
    return value


def read_bool(arguments: dict[str, Any], key: str, default: bool = False) -> bool:
    """Extract a boolean from arguments with a default."""
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"field '{key}' must be a boolean")


def read_int(
    arguments: dict[str, Any],
    key: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Extract an integer from arguments with bounds checking."""
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"field '{key}' must be an integer")
    if min_value is not None and value < min_value:
        raise ValueError(f"field '{key}' must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"field '{key}' must be <= {max_value}")
    return value


def read_optional_number(arguments: dict[str, Any], key: str) -> float | None:
    """Extract an optional number (int or float) from arguments."""
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"field '{key}' must be a number")
    return float(value)


def read_exec_type(arguments: dict[str, Any], key: str = "exec_type") -> str | None:
    """Extract and validate an optional execution type filter."""
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


def read_release_stage(
    arguments: dict[str, Any],
    *,
    key: str = "stage",
    default: str | None = "canary",
    required: bool = False,
) -> str | None:
    """Extract and validate an optional release stage (canary/stable)."""
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


def require_str_list(arguments: dict[str, Any], key: str) -> list[str]:
    """Extract a required non-empty list of strings from arguments."""
    value = arguments.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"field '{key}' must be a non-empty array of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"field '{key}' must be a non-empty array of strings")
        normalized.append(item)
    return normalized


def read_optional_conditions(
    arguments: dict[str, Any], key: str
) -> list[str] | dict[str, Any] | None:
    """Extract optional pre/postconditions as object or string array."""
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"field '{key}' must be an object or an array of strings")
            normalized.append(item)
        return normalized
    raise ValueError(f"field '{key}' must be an object or an array of strings")
