"""Unit tests for version management in ship package.

Validates:
- get_version() reads from pyproject.toml dynamically
- /health endpoint returns version
- /meta endpoint returns version
- FastAPI app has correct version
"""

from __future__ import annotations

import re

import httpx
import pytest

from app.main import app, get_version, RUNTIME_VERSION


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
        version = get_version()
        assert SEMVER_RE.match(version), f"Version '{version}' is not valid semver"

    def test_get_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert get_version() == pyproject_version, (
            f"get_version() ({get_version()}) != pyproject.toml ({pyproject_version})"
        )

    def test_runtime_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert RUNTIME_VERSION == pyproject_version

    def test_fastapi_app_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert app.version == pyproject_version


class TestHealthEndpointVersion:
    """Verify /health endpoint returns version."""

    @pytest.mark.asyncio
    async def test_health_returns_version(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "version" in data, "/health response missing 'version' field"
            assert data["version"] == RUNTIME_VERSION

    @pytest.mark.asyncio
    async def test_health_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.get("/health")
            data = resp.json()
            assert data["version"] == pyproject_version


class TestMetaEndpointVersion:
    """Verify /meta endpoint returns version."""

    @pytest.mark.asyncio
    async def test_meta_returns_version(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.get("/meta")
            assert resp.status_code == 200
            data = resp.json()
            runtime = data.get("runtime", {})
            assert "version" in runtime
            assert runtime["version"] == RUNTIME_VERSION
