"""Workspace API tests: CRUD operations and lifecycle.

Purpose: Verify /v1/workspaces API endpoints work correctly.

Test cases from: plans/phase-1.5/workspace-api-implementation.md section 7

Parallel-safe: Yes - each test creates/deletes its own resources.

Note: GC-related tests are in tests/integration/gc/test_workspace_gc.py
to ensure serial execution.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import pytest

from ..conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    CLEANUP_TIMEOUT,
    DEFAULT_PROFILE,
    DEFAULT_TIMEOUT,
    create_sandbox,
    docker_volume_exists,
    e2e_skipif_marks,
)

pytestmark = e2e_skipif_marks


# =============================================================================
# HELPERS
# =============================================================================


@asynccontextmanager
async def create_workspace(
    client: httpx.AsyncClient,
    *,
    size_limit_mb: int | None = None,
    idempotency_key: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Create external workspace with auto-cleanup."""
    body = {}
    if size_limit_mb is not None:
        body["size_limit_mb"] = size_limit_mb

    headers = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    resp = await client.post(
        "/v1/workspaces", json=body, headers=headers, timeout=DEFAULT_TIMEOUT
    )
    assert resp.status_code == 201, f"Create workspace failed: {resp.text}"
    workspace = resp.json()

    try:
        yield workspace
    finally:
        try:
            await client.delete(
                f"/v1/workspaces/{workspace['id']}",
                timeout=CLEANUP_TIMEOUT,
            )
        except httpx.TimeoutException:
            import warnings

            warnings.warn(
                f"Timeout deleting workspace {workspace['id']} during cleanup.",
                stacklevel=2,
            )
        except httpx.HTTPStatusError:
            # 409 during cleanup is expected if still referenced - ignore
            pass


# =============================================================================
# CREATE WORKSPACE TESTS
# =============================================================================


async def test_create_workspace_returns_valid_response():
    """Create external workspace returns required fields with correct format."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_workspace(client) as workspace:
            assert workspace["id"].startswith("ws-")
            assert workspace["managed"] is False
            assert workspace["managed_by_sandbox_id"] is None
            assert workspace["backend"] == "docker_volume"
            assert "size_limit_mb" in workspace
            assert "created_at" in workspace
            assert "last_accessed_at" in workspace


async def test_create_workspace_with_custom_size():
    """Create workspace with custom size_limit_mb."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_workspace(client, size_limit_mb=2048) as workspace:
            assert workspace["size_limit_mb"] == 2048


async def test_create_workspace_idempotency():
    """Create workspace with Idempotency-Key returns same result on retry (D4)."""
    import uuid

    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Use UUID to ensure uniqueness across parallel test runs
        idempotency_key = f"test-ws-idem-{uuid.uuid4().hex}"

        # First request
        resp1 = await client.post(
            "/v1/workspaces",
            json={"size_limit_mb": 1024},
            headers={"Idempotency-Key": idempotency_key},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp1.status_code == 201, f"First request failed: {resp1.text}"
        workspace1 = resp1.json()

        # Second request with same key
        resp2 = await client.post(
            "/v1/workspaces",
            json={"size_limit_mb": 1024},
            headers={"Idempotency-Key": idempotency_key},
            timeout=DEFAULT_TIMEOUT,
        )
        # Should return cached response (could be 200 or 201 depending on implementation)
        assert resp2.status_code in (200, 201), f"Second request failed: {resp2.text}"
        workspace2 = resp2.json()

        assert workspace1["id"] == workspace2["id"]

        # Cleanup
        await client.delete(
            f"/v1/workspaces/{workspace1['id']}", timeout=CLEANUP_TIMEOUT
        )


async def test_create_workspace_size_limit_validation():
    """size_limit_mb must be in range 1-65536 (D5)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Too small
        resp = await client.post(
            "/v1/workspaces",
            json={"size_limit_mb": 0},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 422  # Pydantic validation error

        # Too large
        resp = await client.post(
            "/v1/workspaces",
            json={"size_limit_mb": 100000},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 422


# =============================================================================
# LIST WORKSPACE TESTS
# =============================================================================


async def test_list_workspaces_default_returns_external_only():
    """GET /v1/workspaces defaults to external workspaces only (D1)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create an external workspace
        async with create_workspace(client) as ext_workspace:
            # Create a sandbox (creates managed workspace)
            async with create_sandbox(client) as sandbox:
                # List without managed param - should only show external
                resp = await client.get("/v1/workspaces", timeout=DEFAULT_TIMEOUT)
                assert resp.status_code == 200
                data = resp.json()

                workspace_ids = [w["id"] for w in data["items"]]
                assert ext_workspace["id"] in workspace_ids

                # Managed workspace should NOT be in default list
                for item in data["items"]:
                    assert item["managed"] is False


async def test_list_workspaces_managed_filter():
    """GET /v1/workspaces?managed=true shows managed workspaces (D1)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            managed_ws_id = sandbox["workspace_id"]

            # List with managed=true
            resp = await client.get(
                "/v1/workspaces?managed=true", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 200
            data = resp.json()

            workspace_ids = [w["id"] for w in data["items"]]
            assert managed_ws_id in workspace_ids

            # All items should be managed
            for item in data["items"]:
                assert item["managed"] is True


async def test_list_workspaces_pagination():
    """List workspaces supports pagination."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create multiple workspaces
        workspaces = []
        for _ in range(3):
            resp = await client.post(
                "/v1/workspaces", json={}, timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 201
            workspaces.append(resp.json())

        try:
            # List with limit=2
            resp = await client.get(
                "/v1/workspaces?limit=2", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 200
            data = resp.json()

            # Should have 2 items and a cursor (if there are more)
            assert len(data["items"]) <= 2

        finally:
            # Cleanup
            for ws in workspaces:
                await client.delete(
                    f"/v1/workspaces/{ws['id']}", timeout=CLEANUP_TIMEOUT
                )


# =============================================================================
# GET WORKSPACE TESTS
# =============================================================================


async def test_get_workspace_returns_details():
    """GET /v1/workspaces/{id} returns workspace details."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_workspace(client, size_limit_mb=512) as workspace:
            resp = await client.get(
                f"/v1/workspaces/{workspace['id']}", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["id"] == workspace["id"]
            assert data["managed"] is False
            assert data["size_limit_mb"] == 512


async def test_get_workspace_not_found():
    """GET /v1/workspaces/{id} returns 404 for non-existent workspace."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        resp = await client.get(
            "/v1/workspaces/ws-nonexistent", timeout=DEFAULT_TIMEOUT
        )
        assert resp.status_code == 404


# =============================================================================
# DELETE WORKSPACE TESTS - External Workspace
# =============================================================================


async def test_delete_external_workspace_success():
    """DELETE /v1/workspaces/{id} succeeds for unreferenced external workspace."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create workspace (not using context manager)
        resp = await client.post(
            "/v1/workspaces", json={}, timeout=DEFAULT_TIMEOUT
        )
        assert resp.status_code == 201
        workspace = resp.json()
        workspace_id = workspace["id"]
        volume_name = f"bay-workspace-{workspace_id}"

        # Verify volume exists
        assert docker_volume_exists(volume_name)

        # Delete
        resp = await client.delete(
            f"/v1/workspaces/{workspace_id}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204

        # Verify gone
        await asyncio.sleep(0.5)
        assert not docker_volume_exists(volume_name)


async def test_delete_external_workspace_referenced_by_active_sandbox():
    """DELETE /v1/workspaces/{id} returns 409 when referenced by active sandbox (D3)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create external workspace
        async with create_workspace(client) as workspace:
            workspace_id = workspace["id"]

            # Create sandbox using this workspace
            resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "workspace_id": workspace_id},
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 201
            sandbox = resp.json()

            try:
                # Try to delete workspace - should fail with 409
                resp = await client.delete(
                    f"/v1/workspaces/{workspace_id}", timeout=DEFAULT_TIMEOUT
                )
                assert resp.status_code == 409

                # Verify error has active_sandbox_ids
                error = resp.json()
                assert "active_sandbox_ids" in error.get("error", {}).get(
                    "details", {}
                )
                assert sandbox["id"] in error["error"]["details"]["active_sandbox_ids"]

            finally:
                # Cleanup sandbox
                await client.delete(
                    f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
                )


async def test_delete_external_workspace_after_sandbox_deleted():
    """DELETE /v1/workspaces/{id} succeeds after referencing sandbox is deleted."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create external workspace
        resp = await client.post(
            "/v1/workspaces", json={}, timeout=DEFAULT_TIMEOUT
        )
        assert resp.status_code == 201
        workspace = resp.json()
        workspace_id = workspace["id"]

        # Create sandbox using this workspace
        resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE, "workspace_id": workspace_id},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 201
        sandbox = resp.json()

        # Delete sandbox
        resp = await client.delete(
            f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204

        # Now workspace can be deleted
        resp = await client.delete(
            f"/v1/workspaces/{workspace_id}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204


# =============================================================================
# DELETE WORKSPACE TESTS - Managed Workspace (D2)
# =============================================================================


async def test_delete_managed_workspace_active_sandbox_returns_409():
    """DELETE /v1/workspaces/{id} returns 409 for managed workspace with active sandbox."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            managed_ws_id = sandbox["workspace_id"]

            # Try to delete managed workspace - should fail
            resp = await client.delete(
                f"/v1/workspaces/{managed_ws_id}", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 409


# Note: test_delete_managed_workspace_after_sandbox_soft_deleted is covered
# in unit tests (test_workspace_manager.py) because in the real API flow,
# sandbox delete cascade-deletes the managed workspace, so it won't exist
# for a subsequent API delete call. The D2 decision scenario (orphan workspace
# after sandbox soft-delete) is properly tested at the unit test level.


# =============================================================================
# SANDBOX + WORKSPACE INTEGRATION TESTS
# =============================================================================


async def test_sandbox_with_external_workspace():
    """Create sandbox binding external workspace works correctly."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_workspace(client) as workspace:
            # Create sandbox with this workspace
            resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "workspace_id": workspace["id"]},
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 201
            sandbox = resp.json()

            try:
                assert sandbox["workspace_id"] == workspace["id"]

                # Execute some code to verify workspace works
                exec_resp = await client.post(
                    f"/v1/sandboxes/{sandbox['id']}/python/exec",
                    json={"code": "print('hello')", "timeout": 30},
                    timeout=120.0,
                )
                assert exec_resp.status_code == 200

            finally:
                await client.delete(
                    f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
                )
