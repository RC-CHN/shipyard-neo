"""Path validation and runtime compatibility helpers for Bay APIs.

Bay performs lexical validation before a request is routed.  Ship is still the
final authority because it resolves real filesystem paths and checks symlinks.
All public filesystem paths are normalized to an absolute POSIX path here:
relative input is anchored at ``/workspace``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence

from app.errors import CapabilityNotSupportedError, InvalidPathError

WORKSPACE_ROOT = "/workspace"
DEFAULT_ALLOWED_ROOTS = (WORKSPACE_ROOT, "/tmp")


def _contains_control_character(value: str) -> bool:
    """Return whether a path contains an ASCII or Unicode control character."""
    return any(unicodedata.category(character) == "Cc" for character in value)


def _is_windows_path(value: str) -> bool:
    """Reject Windows syntax instead of treating it as a Linux filename."""
    return "\\" in value or (len(value) >= 2 and value[1] == ":")


def _lexically_normalize_absolute(path: str, *, allow_root: bool = True) -> str:
    """Normalize a POSIX absolute path without resolving filesystem symlinks."""
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if _contains_control_character(path):
        raise ValueError("path contains a control character")
    if _is_windows_path(path):
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


def normalize_allowed_roots(values: Iterable[object]) -> list[str]:
    """Validate configured roots as canonical POSIX absolute paths.

    The shared workspace must always be present because relative public paths
    are defined against it.  Roots are de-duplicated while preserving their
    configured order.
    """
    roots: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("filesystem.allowed_roots entries must be strings")
        normalized = _lexically_normalize_absolute(value, allow_root=False)
        if normalized not in roots:
            roots.append(normalized)

    if WORKSPACE_ROOT not in roots:
        raise ValueError("filesystem.allowed_roots must include /workspace")
    return roots


def _invalid_path(
    *,
    field_name: str,
    reason: str,
    message: str,
    allowed_roots: Sequence[str] | None = None,
) -> InvalidPathError:
    details: dict[str, object] = {"field": field_name, "reason": reason}
    if allowed_roots is not None:
        details["allowed_roots"] = list(allowed_roots)
    return InvalidPathError(message=message, details=details)


def _is_within_root(path: str, root: str) -> bool:
    """Check hierarchy by components, not by a vulnerable string prefix."""
    return path == root or path.startswith(f"{root}/")


def normalize_sandbox_path(
    path: str,
    *,
    allowed_roots: Sequence[str],
    field_name: str = "path",
) -> str:
    """Normalize one public Bay path to a permitted absolute POSIX path.

    Relative paths remain backward compatible and anchor at ``/workspace``.
    Absolute paths must belong to an allowed root after lexical normalization.
    """
    if not isinstance(path, str) or not path:
        raise _invalid_path(
            field_name=field_name,
            reason="empty_path",
            message=f"{field_name} cannot be empty",
            allowed_roots=allowed_roots,
        )
    if _contains_control_character(path):
        raise _invalid_path(
            field_name=field_name,
            reason="control_character",
            message=f"{field_name} contains invalid characters",
            allowed_roots=allowed_roots,
        )
    if _is_windows_path(path):
        raise _invalid_path(
            field_name=field_name,
            reason="windows_path",
            message=f"{field_name} must use a POSIX path",
            allowed_roots=allowed_roots,
        )

    if path.startswith("/"):
        try:
            normalized = _lexically_normalize_absolute(path)
        except ValueError as exc:
            raise _invalid_path(
                field_name=field_name,
                reason="path_traversal",
                message=f"{field_name} escapes the filesystem root",
                allowed_roots=allowed_roots,
            ) from exc
    else:
        parts = WORKSPACE_ROOT.strip("/").split("/")
        workspace_depth = len(parts)
        for component in path.split("/"):
            if component in ("", "."):
                continue
            if component == "..":
                if len(parts) <= workspace_depth:
                    raise _invalid_path(
                        field_name=field_name,
                        reason="path_traversal",
                        message=f"{field_name} escapes workspace boundary",
                        allowed_roots=allowed_roots,
                    )
                parts.pop()
                continue
            parts.append(component)
        normalized = "/" + "/".join(parts)

    if not any(_is_within_root(normalized, root) for root in allowed_roots):
        raise _invalid_path(
            field_name=field_name,
            reason="outside_allowed_roots",
            message=f"{field_name} is outside the allowed roots",
            allowed_roots=allowed_roots,
        )
    return normalized


def validate_sandbox_path(path: str, *, field_name: str = "path") -> str:
    """Normalize a public path using Bay's configured allowed roots."""
    # Import lazily so config models can use normalize_allowed_roots at import
    # time without creating an import cycle.
    from app.config import get_settings

    return normalize_sandbox_path(
        path,
        allowed_roots=get_settings().filesystem.allowed_roots,
        field_name=field_name,
    )


def validate_optional_sandbox_path(
    path: str | None,
    *,
    field_name: str = "path",
) -> str | None:
    """Normalize an optional public sandbox path."""
    if path is None:
        return None
    return validate_sandbox_path(path, field_name=field_name)


def path_for_runtime(
    path: str,
    *,
    accepts_absolute_paths: bool,
    allowed_roots: Sequence[str],
) -> str:
    """Adapt a normalized Bay path to the negotiated Ship path policy.

    New Ships receive an absolute path.  Older Ships only understand paths
    relative to ``/workspace``; they can safely receive a downgraded path only
    when it belongs to that root.  Other roots, such as ``/tmp``, are never
    guessed or rewritten for an old runtime.
    """
    if accepts_absolute_paths:
        if any(_is_within_root(path, root) for root in allowed_roots):
            return path
        raise CapabilityNotSupportedError(
            message="Runtime path policy does not allow this path",
            capability="filesystem.path_policy",
            available=list(allowed_roots),
            allowed_roots=list(allowed_roots),
        )

    if path == WORKSPACE_ROOT:
        return "."
    if path.startswith(f"{WORKSPACE_ROOT}/"):
        return path[len(WORKSPACE_ROOT) + 1 :]

    raise CapabilityNotSupportedError(
        message="Runtime only supports paths relative to /workspace; upgrade Ship for this root",
        capability="filesystem.path_policy",
        available=[WORKSPACE_ROOT],
        allowed_roots=[WORKSPACE_ROOT],
    )


def validate_relative_path(path: str, *, field_name: str = "path") -> str:
    """Validate a legacy relative-only path and return its relative form.

    This compatibility helper remains for callers that explicitly need the old
    shape.  Public Bay capability endpoints use :func:`validate_sandbox_path`.
    """
    if not path:
        raise _invalid_path(
            field_name=field_name,
            reason="empty_path",
            message=f"{field_name} cannot be empty",
        )
    if "\x00" in path:
        raise _invalid_path(
            field_name=field_name,
            reason="null_byte",
            message=f"{field_name} contains invalid characters",
        )
    if path.startswith("/"):
        raise _invalid_path(
            field_name=field_name,
            reason="absolute_path",
            message=f"{field_name} must be a relative path",
        )

    parts: list[str] = []
    for component in path.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not parts:
                raise _invalid_path(
                    field_name=field_name,
                    reason="path_traversal",
                    message=f"{field_name} escapes workspace boundary",
                )
            parts.pop()
            continue
        parts.append(component)
    return "/".join(parts) or "."


def validate_optional_relative_path(
    path: str | None,
    *,
    field_name: str = "path",
) -> str | None:
    """Validate an optional legacy relative-only path."""
    if path is None:
        return None
    return validate_relative_path(path, field_name=field_name)
