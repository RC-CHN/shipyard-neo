"""Sandbox path policy and workspace helpers.

Relative paths are anchored at ``/workspace``.  Absolute paths may target an
explicitly allowed root (``/workspace`` and ``/tmp`` by default).  The parser
performs a lexical check before resolving the real filesystem path, then
checks the resolved path as a second boundary against symlink escapes.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException

# The cargo-backed workspace is fixed for Ship runtimes.
WORKSPACE_ROOT = Path("/workspace")

# Bay injects this JSON array when it starts a Ship container.  The defaults
# keep standalone Ship images compatible with the public path contract.
ALLOWED_ROOTS_ENV = "BAY_FILESYSTEM_ALLOWED_ROOTS_JSON"
DEFAULT_ALLOWED_ROOTS = ("/workspace", "/tmp")


def get_workspace_dir() -> Path:
    """Return the workspace directory, creating it when necessary."""
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT


def _contains_control_character(value: str) -> bool:
    """Return whether *value* contains a Unicode control character."""
    return any(unicodedata.category(char) == "Cc" for char in value)


def _normalize_absolute_posix_path(path: str, *, allow_root: bool = True) -> str:
    """Lexically normalize a POSIX absolute path without touching the disk."""
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if _contains_control_character(path):
        raise ValueError("path contains a control character")
    if "\\" in path or (len(path) >= 2 and path[1] == ":"):
        raise ValueError("Windows paths are not supported")
    if not path.startswith("/"):
        raise ValueError("path must be absolute")

    parts: list[str] = []
    for component in path.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not parts:
                raise ValueError("path escapes the filesystem root")
            parts.pop()
            continue
        parts.append(component)

    normalized = "/" + "/".join(parts)
    if normalized == "/" and not allow_root:
        raise ValueError("filesystem root is not an allowed root")
    return normalized


def _normalize_allowed_roots(values: Iterable[object]) -> tuple[str, ...]:
    """Validate and de-duplicate a configured collection of allowed roots."""
    roots: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("allowed roots must be strings")
        normalized = _normalize_absolute_posix_path(value, allow_root=False)
        if normalized not in roots:
            roots.append(normalized)
    if not roots:
        raise ValueError("at least one allowed root is required")
    return tuple(roots)


def _default_allowed_roots() -> tuple[str, ...]:
    """Return defaults using the current workspace constant.

    Keeping this dynamic makes the helper straightforward to test with a
    temporary workspace while production still resolves to ``/workspace``.
    """
    return _normalize_allowed_roots((str(WORKSPACE_ROOT), "/tmp"))


def get_allowed_roots() -> tuple[str, ...]:
    """Return the active, validated allowed roots.

    Invalid runtime configuration fails closed to the workspace-only policy.
    Bay validates the same value before creating a Ship container, so this is
    primarily a defence-in-depth fallback for a manually started image.
    """
    raw_roots = os.environ.get(ALLOWED_ROOTS_ENV)
    if raw_roots is None:
        return _default_allowed_roots()

    try:
        parsed = json.loads(raw_roots)
        if not isinstance(parsed, list):
            raise ValueError("allowed roots must be a JSON array")
        roots = _normalize_allowed_roots(parsed)
        workspace_root = _normalize_absolute_posix_path(
            str(WORKSPACE_ROOT), allow_root=False
        )
        if workspace_root not in roots:
            raise ValueError("allowed roots must include the workspace")
        return roots
    except (TypeError, ValueError, json.JSONDecodeError):
        return (_normalize_absolute_posix_path(str(WORKSPACE_ROOT), allow_root=False),)


def get_path_policy() -> dict[str, object]:
    """Return the public path-policy metadata advertised by ``/meta``."""
    return {
        "accepts_absolute_paths": True,
        "allowed_roots": list(get_allowed_roots()),
    }


def _invalid_path(
    *,
    reason: str,
    message: str,
    allowed_roots: tuple[str, ...],
) -> HTTPException:
    """Build Ship's structured path-policy error response."""
    return HTTPException(
        status_code=403,
        detail={
            "code": "invalid_path",
            "message": message,
            "details": {
                "reason": reason,
                "allowed_roots": list(allowed_roots),
            },
        },
    )


def _is_within(candidate: Path, root: Path) -> bool:
    """Return whether ``candidate`` is ``root`` or a descendant of it."""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_request_path(path: str, *, workspace_root: Path, allowed_roots: tuple[str, ...]) -> Path:
    """Apply the lexical half of Ship's path policy."""
    if not isinstance(path, str) or not path:
        raise _invalid_path(
            reason="empty_path",
            message="path cannot be empty",
            allowed_roots=allowed_roots,
        )
    if _contains_control_character(path):
        raise _invalid_path(
            reason="control_character",
            message="path contains a control character",
            allowed_roots=allowed_roots,
        )
    if "\\" in path or (len(path) >= 2 and path[1] == ":"):
        raise _invalid_path(
            reason="windows_path",
            message="Windows paths are not supported",
            allowed_roots=allowed_roots,
        )

    workspace_normalized = _normalize_absolute_posix_path(
        str(workspace_root), allow_root=False
    )
    if path.startswith("/"):
        try:
            normalized = _normalize_absolute_posix_path(path)
        except ValueError as exc:
            raise _invalid_path(
                reason="path_traversal",
                message=str(exc),
                allowed_roots=allowed_roots,
            ) from exc
    else:
        parts = workspace_normalized.strip("/").split("/")
        workspace_depth = len(parts)
        for component in path.split("/"):
            if component in ("", "."):
                continue
            if component == "..":
                if len(parts) <= workspace_depth:
                    raise _invalid_path(
                        reason="path_traversal",
                        message="relative path escapes the workspace",
                        allowed_roots=allowed_roots,
                    )
                parts.pop()
                continue
            parts.append(component)
        normalized = "/" + "/".join(parts)

    candidate = Path(normalized)
    lexical_roots = tuple(Path(root) for root in allowed_roots)
    if not any(_is_within(candidate, root) for root in lexical_roots):
        raise _invalid_path(
            reason="outside_allowed_roots",
            message="path is outside the allowed roots",
            allowed_roots=allowed_roots,
        )
    return candidate


def resolve_path(path: str) -> Path:
    """Resolve a client path and enforce Ship's complete path policy.

    The first check is lexical and prevents prefix spoofing such as
    ``/workspace-evil``.  The second check resolves the real path to prevent
    a symlink inside an allowed root from escaping that root.
    """
    workspace_dir = get_workspace_dir()
    allowed_roots = get_allowed_roots()
    candidate = _normalize_request_path(
        path,
        workspace_root=workspace_dir,
        allowed_roots=allowed_roots,
    )

    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_roots = tuple(Path(root).resolve(strict=False) for root in allowed_roots)
    except (OSError, RuntimeError) as exc:
        raise _invalid_path(
            reason="path_resolution_failed",
            message="path could not be resolved safely",
            allowed_roots=allowed_roots,
        ) from exc

    if not any(_is_within(resolved_candidate, root) for root in resolved_roots):
        raise _invalid_path(
            reason="symlink_escape",
            message="resolved path is outside the allowed roots",
            allowed_roots=allowed_roots,
        )
    return resolved_candidate
