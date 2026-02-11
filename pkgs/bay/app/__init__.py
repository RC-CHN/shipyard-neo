"""Bay - Orchestration layer for Ship containers."""

from __future__ import annotations

import tomllib
from pathlib import Path


def _get_version() -> str:
    """Get version from pyproject.toml (single source of truth)."""
    try:
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "unknown")
    except Exception:
        return "unknown"


__version__ = _get_version()
