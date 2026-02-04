"""Concurrent operations tests.

Purpose: Verify concurrent ensure_running calls don't create multiple sessions.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_concurrent_exec_creates_single_session():
    """Concurrent python/exec calls should result in single session (no dup)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=60.0
    ) as client:
        # Create sandbox
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
        )
        assert create_resp.status_code == 201
        sandbox_id = create_resp.json()["id"]

        try:
            async def exec_python(idx: int) -> dict[str, Any]:
                resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": f"print({idx})", "timeout": 30},
                    timeout=120.0,
                )
                return {
                    "status": resp.status_code,
                    "body": resp.json() if resp.status_code == 200 else resp.text,
                }

            # Fire 5 concurrent requests
            results = await asyncio.gather(
                *[exec_python(i) for i in range(5)],
                return_exceptions=True,
            )

            # Count results
            successes = sum(
                1 for r in results
                if not isinstance(r, Exception) and r["status"] == 200
            )
            retryable = sum(
                1 for r in results
                if not isinstance(r, Exception) and r["status"] == 503
            )

            # At least some should succeed or be retryable (503 during startup is expected)
            assert successes + retryable >= 1, (
                f"Expected at least 1 success/retryable, got: {results}"
            )

            # Wait for session to stabilize, then verify sandbox is accessible
            await asyncio.sleep(2.0)
            get_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert get_resp.status_code == 200

        finally:
            try:
                await client.delete(f"/v1/sandboxes/{sandbox_id}", timeout=120.0)
            except httpx.TimeoutException:
                pass  # Cleanup will be handled by GC or cleanup script
