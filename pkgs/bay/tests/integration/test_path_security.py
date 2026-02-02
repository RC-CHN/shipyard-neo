"""E2E tests for path security validation.

Purpose: Verify Bay rejects malicious path inputs with 400 status code.
See: plans/phase-1.5/path-security-validation.md
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestPathSecurityE2E:
    """E2E tests for path security validation."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            # Cleanup
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    # --- Filesystem Read (GET /filesystem/files) ---

    async def test_filesystem_read_rejects_absolute_path(self, sandbox_id: str):
        """GET /filesystem/files?path=/etc/passwd should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                params={"path": "/etc/passwd"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
            assert error.get("details", {}).get("reason") == "absolute_path"

    async def test_filesystem_read_rejects_traversal(self, sandbox_id: str):
        """GET /filesystem/files?path=../file.txt should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                params={"path": "../secret.txt"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
            assert error.get("details", {}).get("reason") == "path_traversal"

    async def test_filesystem_read_rejects_deep_traversal(self, sandbox_id: str):
        """GET /filesystem/files?path=a/../../etc/passwd should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                params={"path": "a/../../etc/passwd"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    # --- Filesystem Write (PUT /filesystem/files) ---

    async def test_filesystem_write_rejects_absolute_path(self, sandbox_id: str):
        """PUT /filesystem/files with path=/tmp/evil should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "/tmp/evil.sh", "content": "#!/bin/bash\nmalicious"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
            assert error.get("details", {}).get("reason") == "absolute_path"

    async def test_filesystem_write_rejects_traversal(self, sandbox_id: str):
        """PUT /filesystem/files with path=../secret should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "../secret.txt", "content": "secret data"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
            assert error.get("details", {}).get("reason") == "path_traversal"

    # --- Filesystem Delete (DELETE /filesystem/files) ---

    async def test_filesystem_delete_rejects_absolute_path(self, sandbox_id: str):
        """DELETE /filesystem/files?path=/etc/passwd should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.delete(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                params={"path": "/etc/passwd"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    # --- Filesystem List (GET /filesystem/directories) ---

    async def test_filesystem_list_rejects_absolute_path(self, sandbox_id: str):
        """GET /filesystem/directories?path=/etc should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/directories",
                params={"path": "/etc"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    async def test_filesystem_list_rejects_traversal(self, sandbox_id: str):
        """GET /filesystem/directories?path=../ should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/directories",
                params={"path": "../"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    # --- Filesystem Download (GET /filesystem/download) ---

    async def test_filesystem_download_rejects_absolute_path(self, sandbox_id: str):
        """GET /filesystem/download?path=/etc/passwd should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/download",
                params={"path": "/etc/passwd"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    # --- Filesystem Upload (POST /filesystem/upload) ---

    async def test_filesystem_upload_rejects_absolute_path(self, sandbox_id: str):
        """POST /filesystem/upload with path=/tmp/evil should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/filesystem/upload",
                files={"file": ("test.txt", b"test content", "text/plain")},
                data={"path": "/tmp/evil.txt"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    async def test_filesystem_upload_rejects_traversal(self, sandbox_id: str):
        """POST /filesystem/upload with path=../evil should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/filesystem/upload",
                files={"file": ("test.txt", b"test content", "text/plain")},
                data={"path": "../../evil.txt"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    # --- Shell Exec (POST /shell/exec) ---

    async def test_shell_exec_rejects_cwd_absolute_path(self, sandbox_id: str):
        """POST /shell/exec with cwd=/etc should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "ls", "cwd": "/etc"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
            assert error.get("details", {}).get("field") == "cwd"

    async def test_shell_exec_rejects_cwd_traversal(self, sandbox_id: str):
        """POST /shell/exec with cwd=../ should return 400."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "ls", "cwd": "../"},
                timeout=30.0,
            )
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
            assert error.get("details", {}).get("reason") == "path_traversal"

    # --- Valid paths that should be allowed ---

    async def test_valid_path_with_internal_traversal_allowed(self, sandbox_id: str):
        """Path like subdir/../file.txt should be allowed (normalized to file.txt)."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # First write a file to ensure it exists
            write_response = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "test_normalize.txt", "content": "normalized path test"},
                timeout=120.0,
            )
            assert write_response.status_code == 200
            
            # Read using path with internal ..
            # The path "subdir/../test_normalize.txt" should normalize to "test_normalize.txt"
            read_response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                params={"path": "subdir/../test_normalize.txt"},
                timeout=30.0,
            )
            # This should succeed (path normalizes correctly)
            assert read_response.status_code == 200
            assert read_response.json()["content"] == "normalized path test"

    async def test_hidden_file_allowed(self, sandbox_id: str):
        """Hidden files like .gitignore should be allowed."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": ".gitignore", "content": "*.pyc\n__pycache__/"},
                timeout=120.0,
            )
            assert response.status_code == 200

    async def test_shell_exec_cwd_none_allowed(self, sandbox_id: str):
        """Shell exec with cwd=None should use default /workspace."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "pwd"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "/workspace" in result["output"]

    async def test_shell_exec_cwd_relative_allowed(self, sandbox_id: str):
        """Shell exec with valid relative cwd should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # First create a directory
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "subdir/dummy.txt", "content": "test"},
                timeout=120.0,
            )
            
            # Run command in that directory
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "pwd", "cwd": "subdir"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "subdir" in result["output"]
