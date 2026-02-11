"""Unit tests for version management across bay package.

Validates:
- Version is read dynamically from pyproject.toml (single source of truth)
- __version__ in app/__init__.py matches pyproject.toml
- /health endpoint returns version
- FastAPI app has correct version
"""

from __future__ import annotations

import re

import httpx
import pytest

import app as app_pkg
from app import main as main_module


def _read_pyproject_version() -> str:
    """Read version directly from pyproject.toml for comparison."""
    import tomllib
    from pathlib import Path

    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")


class TestVersionConsistency:
    """Verify that all version sources agree."""

    def test_version_is_valid_semver(self):
        assert SEMVER_RE.match(app_pkg.__version__), (
            f"__version__ '{app_pkg.__version__}' is not valid semver"
        )

    def test_init_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert app_pkg.__version__ == pyproject_version, (
            f"app.__version__ ({app_pkg.__version__}) != pyproject.toml ({pyproject_version})"
        )

    def test_fastapi_app_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        app = main_module.create_app()
        assert app.version == pyproject_version, (
            f"FastAPI version ({app.version}) != pyproject.toml ({pyproject_version})"
        )


class TestHealthEndpointVersion:
    """Verify /health endpoint returns version."""

    @pytest.mark.asyncio
    async def test_health_returns_version(self):
        app = main_module.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "version" in data, "/health response missing 'version' field"
            assert data["version"] == app_pkg.__version__

    @pytest.mark.asyncio
    async def test_health_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        app = main_module.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.get("/health")
            data = resp.json()
            assert data["version"] == pyproject_version
