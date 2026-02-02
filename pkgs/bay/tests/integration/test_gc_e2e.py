"""E2E: GC (Garbage Collection) tests.

These tests verify GC behavior using the Admin API for deterministic triggering.

Prerequisites (same as other E2E tests):
- Docker daemon running
- ship:latest image available
- Bay server running with BAY_CONFIG_FILE=tests/scripts/docker-host/config.yaml

Strategy:
- Use POST /v1/admin/gc/run to trigger GC manually instead of waiting for automatic GC
- This provides deterministic test behavior without time-based dependencies
- See: plans/phase-1.5/admin-gc-api-design.md
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid

import httpx
import pytest

from .conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    DEFAULT_PROFILE,
    docker_container_exists,
    docker_volume_exists,
    e2e_skipif_marks,
    gc_serial_mark,
    trigger_gc,
)

# GC tests must run serially to avoid interfering with other tests' sandboxes
pytestmark = e2e_skipif_marks + [gc_serial_mark]

# Profile with very short idle_timeout for IdleSessionGC testing
# Defined in tests/scripts/docker-host/config.yaml
SHORT_IDLE_PROFILE = "short-idle-test"


class TestE2EGC:
    """E2E tests for GC behavior using Admin API."""

    async def test_expired_sandbox_gc_deletes_sandbox_and_workspace(self):
        """ExpiredSandboxGC should soft-delete the sandbox and delete managed workspace volume."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with very short TTL (1 second)
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 1},
            )
            assert create_resp.status_code == 201, create_resp.text
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            workspace_id = sandbox["workspace_id"]
            volume_name = f"bay-workspace-{workspace_id}"

            # Volume should exist immediately after create
            assert docker_volume_exists(volume_name), f"expected volume to exist: {volume_name}"

            # Wait for TTL to expire
            await asyncio.sleep(1.2)

            # Verify sandbox status is EXPIRED before GC
            status_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert status_resp.status_code == 200
            assert status_resp.json()["status"] == "expired", "Sandbox should be expired before GC"

            # Trigger GC manually
            gc_result = await trigger_gc(client)
            
            # Find expired_sandbox task result
            expired_sandbox_result = next(
                (r for r in gc_result["results"] if r["task_name"] == "expired_sandbox"),
                None,
            )
            assert expired_sandbox_result is not None, "expired_sandbox task should have run"
            assert expired_sandbox_result["cleaned_count"] >= 1, (
                f"Expected at least 1 sandbox cleaned, got {expired_sandbox_result}"
            )

            # Verify sandbox is now deleted (404)
            final_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert final_resp.status_code == 404, f"Sandbox should be deleted, got {final_resp.status_code}"

            # Volume should be deleted by cascade delete
            assert not docker_volume_exists(volume_name), f"Volume {volume_name} should be deleted"

    async def test_idle_session_gc_reclaims_compute_and_allows_recreate(self):
        """IdleSessionGC should destroy sessions and clear idle_expires_at/current_session_id.

        Uses the 'short-idle-test' profile with idle_timeout=2s to let the session
        expire naturally, then triggers GC manually to clean it up.
        
        Note: In concurrent test execution, another GC cycle might clean up this
        session before our explicit trigger. We verify the final state (sandbox idle)
        rather than the exact cleaned_count.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with SHORT_IDLE_PROFILE (idle_timeout=2s)
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": SHORT_IDLE_PROFILE},
            )
            assert create_resp.status_code == 201, create_resp.text
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]

            try:
                # Trigger ensure_running by executing python
                exec_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print('hello')", "timeout": 30},
                    timeout=120.0,
                )
                assert exec_resp.status_code == 200, exec_resp.text

                # Verify sandbox is now ready (running session) with idle_expires_at set
                status_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert status_resp.status_code == 200
                status_data = status_resp.json()
                assert status_data["status"] == "ready", f"Expected ready, got {status_data}"
                assert status_data["idle_expires_at"] is not None, "idle_expires_at should be set"

                # Wait for idle timeout to expire (idle_timeout=2s + buffer)
                await asyncio.sleep(3)

                # Trigger GC manually to clean up idle session
                # In concurrent tests, another GC might have already cleaned this up
                gc_result = await trigger_gc(client)
                
                # Find idle_session task result (should have run, even if cleaned_count=0)
                idle_session_result = next(
                    (r for r in gc_result["results"] if r["task_name"] == "idle_session"),
                    None,
                )
                assert idle_session_result is not None, "idle_session task should have run"
                # Note: Don't assert cleaned_count >= 1 because another concurrent GC
                # might have already cleaned this session

                # Verify sandbox is now idle (session destroyed, idle_expires_at cleared)
                # This is the key invariant we're testing
                final_status = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert final_status.status_code == 200
                final_data = final_status.json()
                assert final_data["status"] == "idle", f"Expected idle, got {final_data}"
                assert final_data["idle_expires_at"] is None, "idle_expires_at should be cleared"

                # After GC, another exec should recreate compute successfully
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(1+1)", "timeout": 30},
                    timeout=120.0,
                )
                assert exec2.status_code == 200, exec2.text
                assert exec2.json()["success"] is True

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_orphan_container_gc_deletes_trusted_orphan(self):
        """OrphanContainerGC (strict) should delete a trusted container without DB session.
        
        Note: In concurrent test execution, another GC cycle might clean up this
        container before our explicit trigger. We verify the final state (container deleted)
        rather than the exact cleaned_count.
        """
        # Must match tests/scripts/docker-host/config.yaml
        instance_id = "bay-e2e"

        orphan_suffix = uuid.uuid4().hex[:8]
        session_id = f"sess-orphan-{orphan_suffix}"
        container_name = f"bay-session-{session_id}"

        # Create a long-running container using ship:latest (python should exist)
        # Labels must satisfy OrphanContainerGC strict checks.
        labels = {
            "bay.session_id": session_id,
            "bay.sandbox_id": f"sandbox-orphan-{orphan_suffix}",
            "bay.workspace_id": f"ws-orphan-{orphan_suffix}",
            "bay.instance_id": instance_id,
            "bay.managed": "true",
        }

        label_args = []
        for k, v in labels.items():
            label_args += ["--label", f"{k}={v}"]

        # Create container
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                *label_args,
                "ship:latest",
                "python",
                "-c",
                "import time; time.sleep(600)",
            ],
            check=True,
            capture_output=True,
        )

        try:
            assert docker_container_exists(container_name), "container should exist before GC"

            # Trigger GC manually
            async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
                gc_result = await trigger_gc(client)
                
                # Find orphan_container task result
                orphan_result = next(
                    (r for r in gc_result["results"] if r["task_name"] == "orphan_container"),
                    None,
                )
                assert orphan_result is not None, "orphan_container task should have run"
                # Note: Don't assert cleaned_count >= 1 because another concurrent GC
                # might have already cleaned this container

            # Verify container is now deleted - this is the key invariant we're testing
            assert not docker_container_exists(container_name), (
                f"Container {container_name} should be deleted by OrphanContainerGC"
            )

        finally:
            # Cleanup in case GC didn't delete (or test failed early)
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                check=False,
                capture_output=True,
            )

    async def test_orphan_container_gc_skips_untrusted_container(self):
        """OrphanContainerGC should NOT delete containers with wrong instance_id.
        
        Containers with a different instance_id are not in the scan scope at all,
        so they won't appear in skipped_count. The key assertion is that the
        container still exists after GC.
        """
        orphan_suffix = uuid.uuid4().hex[:8]
        session_id = f"sess-untrusted-{orphan_suffix}"
        container_name = f"bay-session-{session_id}"

        # Create container with WRONG instance_id (different from bay-e2e)
        labels = {
            "bay.session_id": session_id,
            "bay.sandbox_id": f"sandbox-untrusted-{orphan_suffix}",
            "bay.workspace_id": f"ws-untrusted-{orphan_suffix}",
            "bay.instance_id": "other-instance",  # Different from bay-e2e
            "bay.managed": "true",
        }

        label_args = []
        for k, v in labels.items():
            label_args += ["--label", f"{k}={v}"]

        # Create container
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                *label_args,
                "ship:latest",
                "python",
                "-c",
                "import time; time.sleep(600)",
            ],
            check=True,
            capture_output=True,
        )

        try:
            assert docker_container_exists(container_name), "container should exist before GC"

            # Trigger GC manually
            async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
                gc_result = await trigger_gc(client)
                
                # Container should still exist (not deleted due to wrong instance_id)
                # This is the KEY assertion - containers with different instance_id
                # are not in OrphanContainerGC's scan scope, so they're never touched.
                assert docker_container_exists(container_name), (
                    f"Container {container_name} should NOT be deleted (wrong instance_id)"
                )

                # Verify orphan_container task ran successfully
                orphan_result = next(
                    (r for r in gc_result["results"] if r["task_name"] == "orphan_container"),
                    None,
                )
                assert orphan_result is not None, "orphan_container task should have run"
                # Note: Don't assert skipped_count - containers with different instance_id
                # are filtered out at Docker query level, not counted as skipped

        finally:
            # Cleanup
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                check=False,
                capture_output=True,
            )


class TestAdminGCAPI:
    """Tests for Admin GC API endpoints."""

    async def test_gc_status_endpoint(self):
        """GET /v1/admin/gc/status should return current configuration."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get("/v1/admin/gc/status")
            assert response.status_code == 200
            
            data = response.json()
            assert "enabled" in data
            assert "is_running" in data
            assert "instance_id" in data
            assert "tasks" in data
            assert data["instance_id"] == "bay-e2e"

    async def test_gc_run_requires_auth(self):
        """POST /v1/admin/gc/run should require authentication."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL) as client:
            response = await client.post("/v1/admin/gc/run")
            assert response.status_code == 401

    async def test_gc_run_returns_results(self):
        """POST /v1/admin/gc/run should return structured results."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Use trigger_gc which handles 423 retries
            data = await trigger_gc(client)
            
            assert "results" in data
            assert "total_cleaned" in data
            assert "total_errors" in data
            assert "duration_ms" in data
            assert isinstance(data["results"], list)
            assert isinstance(data["duration_ms"], int)
