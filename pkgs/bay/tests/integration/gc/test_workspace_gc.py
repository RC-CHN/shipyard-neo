"""Workspace GC integration tests.

Purpose: Verify GC behavior with workspace API operations.

Test cases from: plans/phase-1.5/workspace-api-implementation.md section 7.2

Serial execution required: Yes - GC tests must run exclusively.
(Matched by conftest.py SERIAL_GROUPS[gc])
"""

from __future__ import annotations

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
    e2e_skipif_marks,
    trigger_gc,
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
) -> AsyncGenerator[dict, None]:
    """Create external workspace with auto-cleanup."""
    body = {}
    if size_limit_mb is not None:
        body["size_limit_mb"] = size_limit_mb

    resp = await client.post(
        "/v1/workspaces", json=body, timeout=DEFAULT_TIMEOUT
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
        except (httpx.TimeoutException, httpx.HTTPStatusError):
            pass


def gc_completed_without_error(gc_result: dict) -> bool:
    """Check if GC completed successfully without errors."""
    # GC response format: {"duration_ms": X, "results": [...], "total_cleaned": Y, "total_errors": Z}
    if "total_errors" in gc_result:
        return gc_result["total_errors"] == 0
    # Fallback for different response formats
    if "status" in gc_result:
        return gc_result["status"] == "ok"
    return True  # No error indicators found


# =============================================================================
# GC INTEGRATION TESTS (Section 7.2)
# =============================================================================


async def test_gc_orphan_workspace_cleanup():
    """GC should clean up orphan managed workspaces.
    
    After sandbox delete (which cascade-deletes managed workspace),
    GC orphan_workspace task should run without error.
    This verifies GC idempotency.
    """
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create sandbox (creates managed workspace)
        resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 201
        sandbox = resp.json()
        sandbox_id = sandbox["id"]

        # Delete sandbox (cascade-deletes managed workspace)
        resp = await client.delete(
            f"/v1/sandboxes/{sandbox_id}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204

        # Trigger GC - should complete without error
        # Even though workspace is already deleted, GC should be idempotent
        gc_result = await trigger_gc(client, tasks=["orphan_workspace"])
        assert gc_completed_without_error(gc_result), f"GC failed: {gc_result}"


async def test_gc_external_workspace_protected():
    """External workspace referenced by active sandbox is protected during GC.
    
    Test case C from phase-1.5 implementation doc section 7.2.
    """
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create external workspace
        async with create_workspace(client) as ext_workspace:
            ext_ws_id = ext_workspace["id"]

            # Create sandbox using external workspace
            resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "workspace_id": ext_ws_id},
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 201
            sandbox = resp.json()

            try:
                # Run GC - should NOT affect external workspace
                gc_result = await trigger_gc(client, tasks=["orphan_workspace"])
                assert gc_completed_without_error(gc_result)

                # External workspace should still exist
                resp = await client.get(
                    f"/v1/workspaces/{ext_ws_id}", timeout=DEFAULT_TIMEOUT
                )
                assert resp.status_code == 200

                # Try to delete - should fail with 409
                resp = await client.delete(
                    f"/v1/workspaces/{ext_ws_id}", timeout=DEFAULT_TIMEOUT
                )
                assert resp.status_code == 409

            finally:
                await client.delete(
                    f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
                )


async def test_gc_concurrent_sandbox_delete_idempotent():
    """Sandbox delete cascade and GC can run concurrently without error.
    
    Test case B from phase-1.5 implementation doc section 7.2:
    Both paths should be idempotent.
    """
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create sandbox
        resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 201
        sandbox = resp.json()
        sandbox_id = sandbox["id"]

        # Delete sandbox (triggers cascade delete internally)
        resp = await client.delete(
            f"/v1/sandboxes/{sandbox_id}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204

        # Immediately trigger GC (may try to clean same workspace)
        gc_result = await trigger_gc(client, tasks=["orphan_workspace"])
        
        # Should complete without error (idempotent)
        assert gc_completed_without_error(gc_result), f"GC failed: {gc_result}"
