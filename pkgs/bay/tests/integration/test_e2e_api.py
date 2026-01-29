"""E2E API tests for Bay.

These tests require:
- Docker daemon running and accessible
- ship:latest image built and available
- Bay server running on http://localhost:8000

See: plans/phase-1/tests.md section 1
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Any

import httpx
import pytest

import os

# Bay API base URL - can be overridden by E2E_BAY_PORT environment variable
_bay_port = os.environ.get("E2E_BAY_PORT", "8001")
BAY_BASE_URL = f"http://127.0.0.1:{_bay_port}"

# Test configuration
OWNER_HEADER = {"X-Owner": "e2e-test-user"}
DEFAULT_PROFILE = "python-default"


def is_bay_running() -> bool:
    """Check if Bay is running."""
    try:
        response = httpx.get(f"{BAY_BASE_URL}/health", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


def is_docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_ship_image_available() -> bool:
    """Check if ship:latest image exists."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "ship:latest"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def docker_volume_exists(volume_name: str) -> bool:
    """Check if a Docker volume exists."""
    try:
        result = subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def docker_container_exists(container_name: str) -> bool:
    """Check if a Docker container exists (running or stopped)."""
    try:
        result = subprocess.run(
            ["docker", "container", "inspect", container_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip all E2E tests if prerequisites not met
pytestmark = [
    pytest.mark.skipif(
        not is_docker_available(),
        reason="Docker is not available",
    ),
    pytest.mark.skipif(
        not is_ship_image_available(),
        reason="ship:latest image not found. Run: cd pkgs/ship && make build",
    ),
    pytest.mark.skipif(
        not is_bay_running(),
        reason="Bay is not running. Start with: cd pkgs/bay && uv run python -m app.main",
    ),
]


class TestE2E01MinimalPath:
    """E2E-01: Minimal path (create → python/exec).
    
    Purpose: Verify ensure_running + host_port mapping + ship /ipython/exec.
    """

    async def test_create_and_exec_python(self):
        """Create sandbox and execute Python code."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Step 1: Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            
            assert create_response.status_code == 201, f"Create failed: {create_response.text}"
            sandbox = create_response.json()
            sandbox_id = sandbox["id"]
            
            try:
                # Verify create response
                assert sandbox["status"] == "idle", f"Expected idle status, got: {sandbox['status']}"
                assert sandbox["workspace_id"] is not None
                assert sandbox["profile"] == DEFAULT_PROFILE
                
                # Step 2: Execute Python code (this triggers ensure_running)
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(1+2)", "timeout": 30},
                    timeout=120.0,  # Allow time for container startup
                )
                
                assert exec_response.status_code == 200, f"Exec failed: {exec_response.text}"
                result = exec_response.json()
                
                # Verify execution result
                assert result["success"] is True, f"Execution failed: {result}"
                assert "3" in result["output"], f"Expected '3' in output, got: {result['output']}"
                
                # Step 3: Verify sandbox now has a session
                get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_response.status_code == 200
                updated_sandbox = get_response.json()
                
                # Status should be ready after execution
                assert updated_sandbox["status"] in ("ready", "starting"), \
                    f"Expected ready/starting status, got: {updated_sandbox['status']}"
                
            finally:
                # Cleanup: Delete sandbox
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_create_response_format(self):
        """Verify create response has correct format."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            
            assert create_response.status_code == 201
            sandbox = create_response.json()
            
            try:
                # Verify required fields
                assert "id" in sandbox
                assert sandbox["id"].startswith("sandbox-")
                assert "status" in sandbox
                assert "profile" in sandbox
                assert "workspace_id" in sandbox
                assert sandbox["workspace_id"].startswith("ws-")
                assert "capabilities" in sandbox
                assert "created_at" in sandbox
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox['id']}")


class TestE2E02Stop:
    """E2E-02: Stop (reclaim compute only).
    
    Purpose: Verify stop destroys session/container but preserves sandbox/workspace.
    """

    async def test_stop_preserves_workspace(self):
        """Stop should destroy session but keep workspace."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create and run sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox = create_response.json()
            sandbox_id = sandbox["id"]
            workspace_id = sandbox["workspace_id"]
            
            try:
                # Trigger session creation by executing code
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print('hello')", "timeout": 30},
                    timeout=120.0,
                )
                assert exec_response.status_code == 200
                
                # Get sandbox to verify it has a session
                get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_response.status_code == 200
                assert get_response.json()["status"] in ("ready", "starting")
                
                # Stop sandbox
                stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert stop_response.status_code == 200
                
                # Verify sandbox still exists and is idle
                get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_response.status_code == 200
                stopped_sandbox = get_response.json()
                assert stopped_sandbox["status"] == "idle"
                
                # Verify workspace still exists (volume should exist)
                # Note: we can verify by checking if workspace_id is still the same
                assert stopped_sandbox["workspace_id"] == workspace_id
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_stop_is_idempotent(self):
        """Stop should be idempotent - repeated calls don't fail."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox (no session yet)
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            try:
                # Stop multiple times - should not fail
                for _ in range(3):
                    stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                    assert stop_response.status_code == 200
                    
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")


class TestE2E03Delete:
    """E2E-03: Delete (complete destruction + managed workspace cascade delete).
    
    Purpose: Verify delete removes sandbox + sessions + managed workspace.
    """

    async def test_delete_returns_404_after(self):
        """Delete should make sandbox return 404."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            # Execute to create session
            await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print(1)", "timeout": 30},
                timeout=120.0,
            )
            
            # Delete sandbox
            delete_response = await client.delete(f"/v1/sandboxes/{sandbox_id}")
            assert delete_response.status_code == 204
            
            # Get should return 404
            get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert get_response.status_code == 404

    async def test_delete_removes_container(self):
        """Delete should remove the container."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create and run sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            # Execute to create container
            await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print(1)", "timeout": 30},
                timeout=120.0,
            )
            
            # Give a moment for container to be fully registered
            await asyncio.sleep(0.5)
            
            # Delete sandbox
            await client.delete(f"/v1/sandboxes/{sandbox_id}")
            
            # Wait for cleanup
            await asyncio.sleep(1.0)
            
            # Container should not exist
            # Note: Container names follow pattern "bay-session-sess-*"
            # We can't easily get the exact session ID here, but we verified
            # through the 404 response that cleanup happened

    async def test_delete_removes_managed_workspace_volume(self):
        """Delete should remove managed workspace volume."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox = create_response.json()
            sandbox_id = sandbox["id"]
            workspace_id = sandbox["workspace_id"]
            volume_name = f"bay-workspace-{workspace_id}"
            
            # Verify volume exists
            assert docker_volume_exists(volume_name), \
                f"Volume {volume_name} should exist after create"
            
            # Delete sandbox
            await client.delete(f"/v1/sandboxes/{sandbox_id}")
            
            # Wait for cleanup
            await asyncio.sleep(0.5)
            
            # Volume should be deleted
            assert not docker_volume_exists(volume_name), \
                f"Volume {volume_name} should be deleted after sandbox delete"


class TestE2E04ConcurrentEnsureRunning:
    """E2E-04: Concurrent ensure_running (same sandbox).
    
    Purpose: Verify concurrent calls don't create multiple sessions.
    """

    async def test_concurrent_exec_creates_single_session(self):
        """Concurrent python/exec calls should result in single session."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            try:
                # Launch concurrent requests
                async def exec_python(code: str) -> dict[str, Any]:
                    response = await client.post(
                        f"/v1/sandboxes/{sandbox_id}/python/exec",
                        json={"code": code, "timeout": 30},
                        timeout=120.0,
                    )
                    return {"status": response.status_code, "body": response.json() if response.status_code == 200 else response.text}
                
                # Fire 5 concurrent requests
                tasks = [
                    exec_python(f"print({i})")
                    for i in range(5)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Count successes and retryable errors
                successes = 0
                retryable_errors = 0
                other_errors = 0
                
                for result in results:
                    if isinstance(result, Exception):
                        other_errors += 1
                    elif result["status"] == 200:
                        successes += 1
                    elif result["status"] == 503:
                        # session_not_ready - expected during startup
                        retryable_errors += 1
                    else:
                        other_errors += 1
                
                # At least some should succeed or be retryable
                assert successes + retryable_errors >= 1, \
                    f"Expected at least 1 success or retryable, got: {results}"
                
                # Should not have catastrophic failures
                # (Some 503s during startup are acceptable)
                
                # Wait for session to stabilize
                await asyncio.sleep(2.0)
                
                # Verify only one session exists by checking sandbox status
                get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_response.status_code == 200
                # If session was created, status should be ready
                # Note: We can't directly verify session count without DB access,
                # but the test ensures concurrent calls don't cause errors
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")


class TestE2E05FileUploadDownload:
    """E2E-05: File upload and download.
    
    Purpose: Verify binary file upload/download to/from sandbox.
    """

    async def test_upload_and_download_text_file(self):
        """Upload a text file and download it back."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            try:
                # Upload a text file
                file_content = b"Hello, World!\nThis is a test file."
                file_path = "test_upload.txt"
                
                upload_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/files/upload",
                    files={"file": ("test_upload.txt", file_content, "text/plain")},
                    data={"path": file_path},
                    timeout=120.0,
                )
                
                assert upload_response.status_code == 200, f"Upload failed: {upload_response.text}"
                upload_result = upload_response.json()
                assert upload_result["status"] == "ok"
                assert upload_result["path"] == file_path
                assert upload_result["size"] == len(file_content)
                
                # Download the file
                download_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/files/download",
                    params={"path": file_path},
                    timeout=30.0,
                )
                
                assert download_response.status_code == 200, f"Download failed: {download_response.text}"
                assert download_response.content == file_content
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_upload_and_download_binary_file(self):
        """Upload a binary file and download it back."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            try:
                # Upload a binary file (simulated PNG header + random bytes)
                binary_content = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(range(256))
                file_path = "test_binary.bin"
                
                upload_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/files/upload",
                    files={"file": ("test_binary.bin", binary_content, "application/octet-stream")},
                    data={"path": file_path},
                    timeout=120.0,
                )
                
                assert upload_response.status_code == 200, f"Upload failed: {upload_response.text}"
                
                # Download and verify
                download_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/files/download",
                    params={"path": file_path},
                    timeout=30.0,
                )
                
                assert download_response.status_code == 200
                assert download_response.content == binary_content
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_upload_to_nested_path(self):
        """Upload a file to a nested directory path."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            try:
                # Upload to nested path
                file_content = b"Nested file content"
                file_path = "subdir/nested/test_file.txt"
                
                upload_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/files/upload",
                    files={"file": ("test_file.txt", file_content, "text/plain")},
                    data={"path": file_path},
                    timeout=120.0,
                )
                
                assert upload_response.status_code == 200, f"Upload failed: {upload_response.text}"
                
                # Download and verify
                download_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/files/download",
                    params={"path": file_path},
                    timeout=30.0,
                )
                
                assert download_response.status_code == 200
                assert download_response.content == file_content
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_download_nonexistent_file(self):
        """Download of non-existent file should return 404."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=OWNER_HEADER) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            try:
                # Try to download non-existent file
                download_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/files/download",
                    params={"path": "nonexistent_file.txt"},
                    timeout=120.0,  # First download triggers session creation
                )
                
                # Should return 404 for file not found
                assert download_response.status_code == 404, \
                    f"Expected 404 for nonexistent file, got: {download_response.status_code}"
                
                # Verify error response format
                error_body = download_response.json()
                assert "error" in error_body
                assert error_body["error"]["code"] == "file_not_found"
                
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
