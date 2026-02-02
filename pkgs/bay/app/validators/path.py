"""Path validation utilities for Bay API.

Bay performs syntactic validation with path normalization.
Ship performs full semantic validation (resolve symlinks, etc.).

Design: Option B - Allow paths that don't escape after normalization.
See: plans/phase-1.5/path-security-validation.md
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.errors import InvalidPathError


def validate_relative_path(path: str, *, field_name: str = "path") -> str:
    """Validate and normalize path to ensure it stays within workspace.

    Rules:
    1. Must not be empty
    2. Must not be absolute (start with /)
    3. Must not contain null bytes
    4. After normalization, must not escape workspace (start with ..)

    Args:
        path: Path to validate
        field_name: Name of field for error messages

    Returns:
        The normalized path if valid

    Raises:
        InvalidPathError: If validation fails

    Examples:
        >>> validate_relative_path("file.txt")
        'file.txt'
        >>> validate_relative_path("subdir/../file.txt")
        'file.txt'  # normalized
        >>> validate_relative_path("./a/b/../c.txt")
        'a/c.txt'  # normalized
        >>> validate_relative_path("../file.txt")
        InvalidPathError  # escapes workspace
    """
    if not path:
        raise InvalidPathError(
            message=f"{field_name} cannot be empty",
            details={"field": field_name, "reason": "empty_path"},
        )

    # Check for null bytes (injection attack)
    if "\x00" in path:
        raise InvalidPathError(
            message=f"{field_name} contains invalid characters",
            details={"field": field_name, "reason": "null_byte"},
        )

    p = PurePosixPath(path)

    # Check absolute path
    if p.is_absolute():
        raise InvalidPathError(
            message=f"{field_name} must be a relative path",
            details={"field": field_name, "reason": "absolute_path"},
        )

    # Normalize path: resolve . and .. components
    # PurePosixPath doesn't have resolve(), so we manually normalize
    parts: list[str] = []
    for part in p.parts:
        if part == ".":
            continue
        elif part == "..":
            if parts:
                parts.pop()
            else:
                # Trying to go above workspace root
                raise InvalidPathError(
                    message=f"{field_name} escapes workspace boundary",
                    details={"field": field_name, "reason": "path_traversal"},
                )
        else:
            parts.append(part)

    # Return normalized path
    if not parts:
        return "."
    return "/".join(parts)


def validate_optional_relative_path(
    path: str | None, *, field_name: str = "path"
) -> str | None:
    """Validate an optional relative path.

    If path is None, returns None without validation.
    Otherwise, validates and normalizes the path.

    Args:
        path: Optional path to validate
        field_name: Name of field for error messages

    Returns:
        The normalized path if valid, or None if path was None

    Raises:
        InvalidPathError: If validation fails
    """
    if path is None:
        return None
    return validate_relative_path(path, field_name=field_name)
