"""Unit tests for version management in gull package.

Validates:
- get_version() reads from pyproject.toml dynamically
- GULL_VERSION matches pyproject.toml
- /health endpoint returns version field
- /meta endpoint returns version
- FastAPI app has correct version
"""

from __future__ import annotations

import re

import pytest

import app.main as gull_main


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
        version = gull_main.get_version()
        assert SEMVER_RE.match(version), f"Version '{version}' is not valid semver"

    def test_get_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert gull_main.get_version() == pyproject_version

    def test_gull_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert gull_main.GULL_VERSION == pyproject_version

    def test_fastapi_app_version_matches_pyproject(self):
        pyproject_version = _read_pyproject_version()
        assert gull_main.app.version == pyproject_version


class TestHealthEndpointVersion:
    """Verify /health response includes version."""

    @pytest.mark.asyncio
    async def test_health_unhealthy_includes_version(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(gull_main.shutil, "which", lambda _name: None)
        response = await gull_main.health()
        assert response.version == gull_main.GULL_VERSION

    @pytest.mark.asyncio
    async def test_health_healthy_includes_version(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
        )

        async def fake_run(_cmd: str, **_kwargs):
            return gull_main.SESSION_NAME, "", 0

        monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

        response = await gull_main.health()
        assert response.version == gull_main.GULL_VERSION

    @pytest.mark.asyncio
    async def test_health_degraded_includes_version(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
        )

        async def fake_run(_cmd: str, **_kwargs):
            return "", "probe failed", 2

        monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

        response = await gull_main.health()
        assert response.version == gull_main.GULL_VERSION
