"""Sandbox lifecycle tests: create, exec, stop, delete.

Purpose: Verify core sandbox operations work correctly.
Consolidates: test_minimal_path.py, test_stop.py, test_delete.py

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import asyncio

import httpx

from ..conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    DEFAULT_PROFILE,
    create_sandbox,
    docker_volume_exists,
    e2e_skipif_marks,
)

pytestmark = e2e_skipif_marks


# --- Create tests ---


async def test_create_returns_valid_response():
    """Create sandbox returns required fields with correct format."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            assert sandbox["id"].startswith("sandbox-")
            assert sandbox["status"] == "idle"
            assert sandbox["profile"] == DEFAULT_PROFILE
            assert sandbox["cargo_id"].startswith("ws-")
            assert "capabilities" in sandbox
            assert "created_at" in sandbox


async def test_create_and_exec_python():
    """Create sandbox and execute Python code - minimal path."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            # Execute Python (triggers ensure_running)
            exec_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print(1+2)", "timeout": 30},
                timeout=120.0,
            )
            assert exec_resp.status_code == 200, f"Exec failed: {exec_resp.text}"

            result = exec_resp.json()
            assert result["success"] is True
            assert "3" in result["output"]

            # Verify sandbox is now running
            get_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["status"] in ("ready", "starting")


# --- Stop tests ---


async def test_stop_preserves_cargo():
    """Stop destroys session but keeps sandbox/cargo."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]
            cargo_id = sandbox["cargo_id"]

            # Start session
            await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print('hello')", "timeout": 30},
                timeout=120.0,
            )

            # Stop
            stop_resp = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
            assert stop_resp.status_code == 200

            # Verify idle status, same workspace
            get_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert get_resp.status_code == 200
            stopped = get_resp.json()
            assert stopped["status"] == "idle"
            assert stopped["cargo_id"] == cargo_id


async def test_stop_is_idempotent():
    """Stop is idempotent - repeated calls don't fail."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            for _ in range(3):
                resp = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert resp.status_code == 200


# --- Delete tests ---


async def test_delete_returns_404_after():
    """Delete makes sandbox return 404."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Create sandbox (not using context manager - we're testing delete explicitly)
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
        )
        assert create_resp.status_code == 201
        sandbox_id = create_resp.json()["id"]

        # Execute to create session
        await client.post(
            f"/v1/sandboxes/{sandbox_id}/python/exec",
            json={"code": "print(1)", "timeout": 30},
            timeout=120.0,
        )

        # Delete
        del_resp = await client.delete(f"/v1/sandboxes/{sandbox_id}")
        assert del_resp.status_code == 204

        # Get should return 404
        get_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
        assert get_resp.status_code == 404


async def test_delete_removes_managed_cargo_volume():
    """Delete removes managed cargo volume."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Create
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
        )
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        sandbox_id = sandbox["id"]
        cargo_id = sandbox["cargo_id"]
        volume_name = f"bay-cargo-{cargo_id}"

        # Verify volume exists
        assert docker_volume_exists(volume_name), f"Volume {volume_name} should exist"

        # Delete
        await client.delete(f"/v1/sandboxes/{sandbox_id}")
        await asyncio.sleep(0.5)

        # Volume should be gone
        assert not docker_volume_exists(volume_name), f"Volume {volume_name} should be deleted"
