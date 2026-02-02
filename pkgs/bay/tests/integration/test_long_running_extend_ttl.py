"""E2E-13: Long Running Task with TTL Extension (Long Job workflow) tests.

Purpose: Simulate a developer/data engineer running long-duration tasks:
- Create sandbox with short initial TTL
- Execute task that may exceed initial TTL estimate
- Extend TTL before expiration to keep task running
- Verify idempotent retry of extend_ttl
- Verify rejection after TTL expiration (cannot resurrect)

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 5
See: plans/phase-1/sandbox-extend-ttl.md for extend_ttl semantics
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestE2E13LongRunningExtendTTL:
    """E2E-13: Long Running Task with TTL Extension (Long Job workflow)."""

    async def test_long_task_workflow_with_extend_ttl(self):
        """Complete workflow: short TTL -> run task -> extend -> continue -> cleanup.
        
        This simulates a real scenario where:
        1. Developer creates sandbox with estimated 2 min TTL
        2. Task starts running
        3. Developer realizes task needs more time
        4. Extends TTL to continue
        5. Task completes successfully
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Step 1: Create sandbox with short TTL (120s) - simulating underestimated duration
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 120},
            )
            assert create_resp.status_code == 201
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            initial_expires_at = sandbox["expires_at"]
            assert initial_expires_at is not None, "TTL sandbox should have expires_at"

            try:
                # Step 2: Start a "long running task" (simulated by simple exec)
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": "import time; print('Task started'); time.sleep(2); print('Phase 1 done')",
                        "timeout": 30,
                    },
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is True
                assert "Phase 1 done" in result1["output"]

                # Step 3: Extend TTL before expiration (add 600s = 10 min)
                extend_key = f"extend-ttl-{uuid.uuid4()}"
                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 600},
                    headers={"Idempotency-Key": extend_key},
                )
                assert extend_resp.status_code == 200
                extended = extend_resp.json()
                assert extended["id"] == sandbox_id
                new_expires_at = extended["expires_at"]
                assert new_expires_at is not None
                assert new_expires_at != initial_expires_at, "expires_at should have changed"

                # Step 4: Verify idempotent retry returns same response
                extend_resp2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 600},
                    headers={"Idempotency-Key": extend_key},
                )
                assert extend_resp2.status_code == 200
                replayed = extend_resp2.json()
                assert replayed["expires_at"] == new_expires_at, \
                    "Idempotent retry should return same expires_at"

                # Step 5: Continue task execution (verify sandbox still works)
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": "print('Phase 2 done - task completed')",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True
                assert "Phase 2 done" in result2["output"]

            finally:
                # Cleanup
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_extend_ttl_rejected_after_expiration(self):
        """After TTL expiration, extend_ttl should be rejected (no resurrection).
        
        This verifies the "sandbox_expired" error behavior:
        - Create sandbox with short TTL (3s)
        - Wait for expiration (3.5s)
        - Attempt to extend -> should get 409 sandbox_expired
        
        Note: TTL and wait times are chosen to ensure expiration occurs before
        GC runs (GC interval is 5s in E2E config). This tests the API-level
        expiration check, not GC deletion behavior.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with short TTL - must expire before GC runs (5s interval)
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 3},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                # Wait for expiration (TTL=3s + buffer)
                # This should happen before GC runs (interval=5s)
                await asyncio.sleep(3.5)

                # Attempt to extend - should fail with 409 sandbox_expired
                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 60},
                )
                assert extend_resp.status_code == 409, \
                    f"Expected 409 sandbox_expired, got {extend_resp.status_code}: {extend_resp.text}"
                error = extend_resp.json()
                assert error["error"]["code"] == "sandbox_expired", \
                    f"Expected sandbox_expired error, got: {error}"

            finally:
                # Note: sandbox may have been deleted by GC at this point, ignore 404
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_extend_ttl_rejected_for_infinite_ttl(self):
        """Extending TTL on a sandbox without TTL (infinite) should be rejected.
        
        This verifies the "sandbox_ttl_infinite" error behavior:
        - Create sandbox without TTL (infinite)
        - Attempt to extend -> should get 409 sandbox_ttl_infinite
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox without TTL (infinite)
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_resp.status_code == 201
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            assert sandbox.get("expires_at") is None, "No-TTL sandbox should have null expires_at"

            try:
                # Attempt to extend - should fail
                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 60},
                )
                assert extend_resp.status_code == 409
                error = extend_resp.json()
                assert error["error"]["code"] == "sandbox_ttl_infinite", \
                    f"Expected sandbox_ttl_infinite error, got: {error}"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_extend_ttl_does_not_affect_running_session(self):
        """extend_ttl should only affect expires_at, not the running session.
        
        This verifies:
        - Session variables persist across extend_ttl call
        - Container remains running (not restarted)
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with TTL
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 300},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                # Define a variable
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "session_marker = 'alive_before_extend'", "timeout": 30},
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                assert exec1.json()["success"] is True

                # Extend TTL
                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 600},
                )
                assert extend_resp.status_code == 200

                # Verify variable still exists (session not restarted)
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(session_marker)", "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True, \
                    f"Variable should persist after extend_ttl: {result2}"
                assert "alive_before_extend" in result2["output"], \
                    "Session variable should be unchanged after extend_ttl"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_multiple_extend_ttl_accumulates(self):
        """Multiple extend_ttl calls with different idempotency keys should each extend.
        
        Note: This tests the non-idempotent case (different keys).
        Each call should extend from the current expires_at.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with short TTL
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 60},
            )
            assert create_resp.status_code == 201
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            first_expires = sandbox["expires_at"]

            try:
                # First extend
                extend1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 60},
                    headers={"Idempotency-Key": f"extend-1-{uuid.uuid4()}"},
                )
                assert extend1.status_code == 200
                second_expires = extend1.json()["expires_at"]
                assert second_expires > first_expires, "First extend should increase expires_at"

                # Second extend (different key)
                extend2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 60},
                    headers={"Idempotency-Key": f"extend-2-{uuid.uuid4()}"},
                )
                assert extend2.status_code == 200
                third_expires = extend2.json()["expires_at"]
                assert third_expires > second_expires, "Second extend should increase expires_at further"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
