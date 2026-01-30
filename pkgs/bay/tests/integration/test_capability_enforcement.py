"""Integration tests for Profile-level capability enforcement.

Tests that the capability enforcement works end-to-end via HTTP API,
blocking requests to capabilities not supported by the sandbox's profile
BEFORE starting any container.

See: plans/phase-1/profile-capability-enforcement.md

Requirements:
- Bay server running with a restricted profile configured
- Docker available
- ship:latest image built

NOTE: These tests require a special test profile to be configured in Bay:
  - id: python-only-test
    capabilities: [python]  # No shell/filesystem access
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL

# Test profile: must be configured in config.yaml for integration tests
RESTRICTED_PROFILE = "python-only-test"


class TestCapabilityEnforcementE2E:
    """End-to-end tests for profile-level capability enforcement."""

    @pytest.fixture
    async def restricted_sandbox_id(self) -> str:
        """Create a sandbox with restricted profile for testing.
        
        Uses python-only-test profile which only allows python capability.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL) as client:
            # Create sandbox with restricted profile
            response = await client.post(
                "/v1/sandboxes",
                json={
                    "profile": RESTRICTED_PROFILE,
                },
                headers=AUTH_HEADERS,
            )
            
            # If profile doesn't exist, skip the test
            if response.status_code == 400 and "profile" in response.text.lower():
                pytest.skip(
                    f"Restricted profile '{RESTRICTED_PROFILE}' not configured. "
                    "Add to config.yaml: profiles: [{id: python-only-test, capabilities: [python]}]"
                )
            
            assert response.status_code == 201, f"Failed to create sandbox: {response.text}"
            data = response.json()
            sandbox_id = data["id"]
            
            yield sandbox_id
            
            # Cleanup
            await client.delete(
                f"/v1/sandboxes/{sandbox_id}",
                headers=AUTH_HEADERS,
            )

    @pytest.mark.asyncio
    async def test_allowed_capability_succeeds(self, restricted_sandbox_id: str):
        """Python execution should succeed on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=60.0) as client:
            response = await client.post(
                f"/v1/sandboxes/{restricted_sandbox_id}/python/exec",
                json={"code": "print('hello')"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 200, f"Expected success: {response.text}"
            data = response.json()
            assert data["success"] is True
            assert "hello" in data["output"]

    @pytest.mark.asyncio
    async def test_shell_capability_denied(self, restricted_sandbox_id: str):
        """Shell execution should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.post(
                f"/v1/sandboxes/{restricted_sandbox_id}/shell/exec",
                json={"command": "echo hello"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400, f"Expected 400, got: {response.status_code}"
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"
            assert data["error"]["details"]["capability"] == "shell"
            assert "python" in data["error"]["details"]["available"]

    @pytest.mark.asyncio
    async def test_filesystem_read_denied(self, restricted_sandbox_id: str):
        """File read should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.get(
                f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/files",
                params={"path": "test.txt"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"
            assert data["error"]["details"]["capability"] == "filesystem"

    @pytest.mark.asyncio
    async def test_filesystem_write_denied(self, restricted_sandbox_id: str):
        """File write should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.put(
                f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/files",
                json={"path": "test.txt", "content": "hello"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"
            assert data["error"]["details"]["capability"] == "filesystem"

    @pytest.mark.asyncio
    async def test_filesystem_list_denied(self, restricted_sandbox_id: str):
        """Directory listing should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.get(
                f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/directories",
                params={"path": "."},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"

    @pytest.mark.asyncio
    async def test_filesystem_delete_denied(self, restricted_sandbox_id: str):
        """File delete should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.delete(
                f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/files",
                params={"path": "test.txt"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"

    @pytest.mark.asyncio
    async def test_upload_denied(self, restricted_sandbox_id: str):
        """File upload should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.post(
                f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/upload",
                files={"file": ("test.txt", b"hello", "text/plain")},
                data={"path": "test.txt"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"

    @pytest.mark.asyncio
    async def test_download_denied(self, restricted_sandbox_id: str):
        """File download should be blocked on python-only profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=10.0) as client:
            response = await client.get(
                f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/download",
                params={"path": "test.txt"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 400
            data = response.json()
            assert data["error"]["code"] == "capability_not_supported"


class TestFullProfileAllowsAll:
    """Verify that full-access profile allows all capabilities."""

    @pytest.fixture
    async def full_sandbox_id(self) -> str:
        """Create a sandbox with default (full) profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL) as client:
            response = await client.post(
                "/v1/sandboxes",
                json={
                    "profile": "python-default",
                },
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 201, f"Failed to create sandbox: {response.text}"
            data = response.json()
            sandbox_id = data["id"]
            
            yield sandbox_id
            
            # Cleanup
            await client.delete(
                f"/v1/sandboxes/{sandbox_id}",
                headers=AUTH_HEADERS,
            )

    @pytest.mark.asyncio
    async def test_python_allowed(self, full_sandbox_id: str):
        """Python execution should work on full profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=60.0) as client:
            response = await client.post(
                f"/v1/sandboxes/{full_sandbox_id}/python/exec",
                json={"code": "print('test')"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_shell_allowed(self, full_sandbox_id: str):
        """Shell execution should work on full profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=60.0) as client:
            response = await client.post(
                f"/v1/sandboxes/{full_sandbox_id}/shell/exec",
                json={"command": "echo test"},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_filesystem_allowed(self, full_sandbox_id: str):
        """Filesystem operations should work on full profile."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, timeout=60.0) as client:
            # First ensure session is started
            await client.post(
                f"/v1/sandboxes/{full_sandbox_id}/python/exec",
                json={"code": "1+1"},
                headers=AUTH_HEADERS,
            )
            
            # Then test filesystem
            response = await client.get(
                f"/v1/sandboxes/{full_sandbox_id}/filesystem/directories",
                params={"path": "."},
                headers=AUTH_HEADERS,
            )
            
            assert response.status_code == 200
