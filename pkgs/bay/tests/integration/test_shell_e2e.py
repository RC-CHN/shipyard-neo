"""E2E tests for shell execution functionality.

Purpose: Verify shell execution works correctly end-to-end via Bay API.
Tests command execution, output capture, error handling, and working directory.

Requirements:
- Bay server running
- Docker available
- ship:latest image built
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestShellExecE2E:
    """E2E tests for shell command execution."""

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

    async def test_shell_exec_echo(self, sandbox_id: str):
        """Simple echo command should succeed."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo 'hello world'"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "hello world" in result["output"]

    async def test_shell_exec_pwd_default_workspace(self, sandbox_id: str):
        """pwd should return /workspace by default."""
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

    async def test_shell_exec_with_relative_cwd(self, sandbox_id: str):
        """Shell exec with relative cwd should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # First create a directory
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "testdir/dummy.txt", "content": "test"},
                timeout=120.0,
            )
            
            # Run pwd in that directory
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "pwd", "cwd": "testdir"},
                timeout=30.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "testdir" in result["output"]

    async def test_shell_exec_ls(self, sandbox_id: str):
        """ls command should list files."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create a test file first
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "test_ls_file.txt", "content": "test content"},
                timeout=120.0,
            )
            
            # List files
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "ls"},
                timeout=30.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "test_ls_file.txt" in result["output"]

    async def test_shell_exec_exit_code_nonzero(self, sandbox_id: str):
        """Command with non-zero exit code should have success=False."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "exit 1"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is False
            assert result["exit_code"] == 1

    async def test_shell_exec_command_not_found(self, sandbox_id: str):
        """Non-existent command should fail."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "this_command_does_not_exist_12345"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is False
            # exit_code should be 127 (command not found) or similar non-zero

    async def test_shell_exec_pipe(self, sandbox_id: str):
        """Piped commands should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo 'hello' | cat"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "hello" in result["output"]

    async def test_shell_exec_multiline_output(self, sandbox_id: str):
        """Commands with multiline output should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo -e 'line1\\nline2\\nline3'"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "line1" in result["output"]
            assert "line2" in result["output"]
            assert "line3" in result["output"]

    async def test_shell_exec_env_variables(self, sandbox_id: str):
        """Environment variables should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo $HOME"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # HOME should be set to /workspace
            assert "/workspace" in result["output"]

    async def test_shell_exec_file_manipulation(self, sandbox_id: str):
        """Shell commands that create/modify files should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create a file via shell
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo 'created by shell' > shell_created.txt"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            assert response.json()["success"] is True
            
            # Verify file was created by reading it
            read_response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                params={"path": "shell_created.txt"},
                timeout=30.0,
            )
            
            assert read_response.status_code == 200
            assert "created by shell" in read_response.json()["content"]


class TestShellExecSecurityE2E:
    """E2E tests for shell execution security."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_shell_cannot_access_host_files(self, sandbox_id: str):
        """Shell commands should not access host filesystem outside container."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Try to read host's /etc/passwd - should return container's version
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "cat /etc/passwd"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            # Command might succeed but shows container's passwd, not host's
            # The key point is it's isolated
            if result["success"]:
                # Container passwd should have shipyard user
                assert "shipyard" in result["output"]

    async def test_shell_runs_as_shipyard_user(self, sandbox_id: str):
        """Shell commands should run as shipyard user."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "whoami"},
                timeout=120.0,
            )
            
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "shipyard" in result["output"]

    async def test_shell_cwd_traversal_blocked(self, sandbox_id: str):
        """Shell exec with path traversal in cwd should be blocked at Bay level."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "ls", "cwd": "../../../etc"},
                timeout=30.0,
            )
            
            # Should be rejected by Bay's path validation
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"

    async def test_shell_absolute_cwd_blocked(self, sandbox_id: str):
        """Shell exec with absolute cwd path should be blocked."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "ls", "cwd": "/etc"},
                timeout=30.0,
            )
            
            # Should be rejected by Bay's path validation
            assert response.status_code == 400
            error = response.json().get("error", {})
            assert error.get("code") == "invalid_path"
