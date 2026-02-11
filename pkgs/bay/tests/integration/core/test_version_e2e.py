"""E2E tests for version consistency across Bay, Ship, and Gull.

Validates:
- Bay /health returns version
- Ship container's /health returns version (via sandbox exec)
- All versions are valid semver and non-empty
- GET /v1/sandboxes/{id} returns containers with version info
"""

from __future__ import annotations

import re

import httpx
import pytest

from tests.integration.conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    DEFAULT_TIMEOUT,
    create_sandbox,
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")


@pytest.mark.asyncio
async def test_bay_health_returns_version():
    """Bay's /health endpoint should include a 'version' field."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        resp = await client.get("/health", timeout=DEFAULT_TIMEOUT)
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data, "/health response missing 'version' field"
        assert data["version"], "version should not be empty"
        assert SEMVER_RE.match(data["version"]), (
            f"version '{data['version']}' is not valid semver"
        )


@pytest.mark.asyncio
async def test_ship_health_returns_version_via_sandbox():
    """Ship container's /health endpoint (probed via shell exec) should return version."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            # Execute a curl to Ship's internal health endpoint
            # Ship runs on port 8123 inside the container
            resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "curl -s http://localhost:8123/health",
                    "timeout": 10,
                },
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 200
            exec_result = resp.json()
            assert exec_result.get("exit_code") == 0, (
                f"curl failed: {exec_result.get('error', '')}"
            )

            # Parse the JSON response from Ship's health endpoint
            # Bay shell exec API returns 'output' (not 'stdout')
            import json

            health_data = json.loads(exec_result["output"])
            assert "version" in health_data, (
                "Ship /health response missing 'version' field"
            )
            assert health_data["version"], "Ship version should not be empty"
            assert SEMVER_RE.match(health_data["version"]), (
                f"Ship version '{health_data['version']}' is not valid semver"
            )


@pytest.mark.asyncio
async def test_ship_meta_returns_version_via_sandbox():
    """Ship container's /meta endpoint should return runtime version."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "curl -s http://localhost:8123/meta",
                    "timeout": 10,
                },
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 200
            exec_result = resp.json()
            assert exec_result.get("exit_code") == 0

            # Bay shell exec API returns 'output' (not 'stdout')
            import json

            meta_data = json.loads(exec_result["output"])
            runtime = meta_data.get("runtime", {})
            assert "version" in runtime, "Ship /meta runtime missing 'version'"
            assert runtime["version"], "Ship /meta version should not be empty"
            assert SEMVER_RE.match(runtime["version"]), (
                f"Ship /meta version '{runtime['version']}' is not valid semver"
            )


@pytest.mark.asyncio
async def test_sandbox_detail_returns_containers_with_version():
    """GET /v1/sandboxes/{id} should include containers with version info.

    After triggering a session (via shell exec), the sandbox detail response
    should contain a 'containers' list with each container's version.
    """
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            # Trigger session creation by executing a simple command
            resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo ok", "timeout": 10},
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 200

            # Now GET sandbox detail - should have containers
            resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}",
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 200
            data = resp.json()

            assert "containers" in data, (
                "GET /v1/sandboxes/{id} response missing 'containers' field"
            )
            containers = data["containers"]
            assert containers is not None, "containers should not be None when session is running"
            assert len(containers) >= 1, "should have at least one container"

            # Verify container structure
            for container in containers:
                assert "name" in container
                assert "runtime_type" in container
                assert "status" in container
                assert "capabilities" in container

                # Version should be populated for running containers
                if container["status"] == "running":
                    assert container.get("version"), (
                        f"Container '{container['name']}' has no version"
                    )
                    assert SEMVER_RE.match(container["version"]), (
                        f"Container version '{container['version']}' is not valid semver"
                    )


@pytest.mark.asyncio
async def test_sandbox_detail_no_containers_when_idle():
    """GET /v1/sandboxes/{id} should NOT include containers when sandbox is idle.

    A freshly created sandbox (no session yet) should have containers=null.
    """
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            # GET sandbox detail immediately (no session triggered)
            resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}",
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 200
            data = resp.json()

            # containers should be null or absent when idle
            containers = data.get("containers")
            assert containers is None, (
                "containers should be null when sandbox is idle (no session)"
            )
