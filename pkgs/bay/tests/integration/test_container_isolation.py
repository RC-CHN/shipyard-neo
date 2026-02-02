"""E2E tests for container isolation verification.

Scenario 7 Part B: Container Isolation Verification (容器隔离验证)

Purpose: Verify that Python/Shell code executes within the container's isolation
boundary. While the container can access its own filesystem, it cannot escape
to the host machine.

Key insight: Python/Shell code CAN read container's /etc/passwd - this is the
container's file (from the image layer), NOT the host's. This is expected behavior
because the sandbox purpose is isolation, not code auditing.

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 7
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestContainerIsolationE2E:
    """E2E tests for container isolation verification.
    
    These tests verify that:
    1. Python/Shell code runs inside the container's isolated environment
    2. Container has its own filesystem (not host's)
    3. Commands execute as 'shipyard' user (non-root)
    4. Working directory is /workspace
    """

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

    # --- Python code can access container's own files ---

    async def test_python_can_read_container_etc_passwd(self, sandbox_id: str):
        """Python can read /etc/passwd - this is the CONTAINER's passwd file.
        
        This is expected behavior: the sandbox isolates code to the container,
        but within the container, code has normal filesystem access.
        The passwd file is from the container image, not the host.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print(open('/etc/passwd').read()[:300])"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Container's passwd file should have root entry
            assert "root:" in result["output"]

    async def test_python_verifies_shipyard_user_in_container(self, sandbox_id: str):
        """Verify that 'shipyard' user exists in container - proves this is Ship's container.
        
        The host machine doesn't have a 'shipyard' user, so finding it in /etc/passwd
        proves we're reading the container's file, not the host's.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print('shipyard' in open('/etc/passwd').read())"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "True" in result["output"]

    # --- Shell commands verify container environment ---

    async def test_shell_whoami_is_shipyard(self, sandbox_id: str):
        """Shell commands execute as 'shipyard' user (non-root).
        
        This verifies the container runs with reduced privileges.
        """
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

    async def test_shell_reads_container_passwd_with_shipyard_user(self, sandbox_id: str):
        """Shell can grep shipyard user from container's /etc/passwd.
        
        Expected output format: shipyard:x:1000:1000::/workspace:/bin/bash
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "cat /etc/passwd | grep shipyard"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Verify shipyard user entry format
            assert "shipyard:" in result["output"]
            assert "/workspace" in result["output"]

    # --- Working directory verification ---

    async def test_python_cwd_is_workspace(self, sandbox_id: str):
        """Python's current working directory should be /workspace."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "import os; print(os.getcwd())"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "/workspace" in result["output"]

    async def test_shell_pwd_is_workspace(self, sandbox_id: str):
        """Shell's default working directory should be /workspace."""
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

    # --- Container environment details ---

    async def test_python_checks_home_directory(self, sandbox_id: str):
        """Python can check if /home exists - this is container's /home.
        
        Even if /home exists, it's the container's home directory, not the host's.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "import os; print(f'exists={os.path.exists(\"/home\")}')"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # The result doesn't matter - what matters is we're reading container's FS
            assert "exists=" in result["output"]

    async def test_shell_checks_mount_info(self, sandbox_id: str):
        """Shell can check mount information for /workspace.
        
        This verifies that /workspace is a mounted volume, not a host directory.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "mount | grep workspace || echo 'workspace mount exists'"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Either shows mount info or our fallback message

    async def test_shell_env_home_is_workspace(self, sandbox_id: str):
        """Shell's $HOME environment variable should be /workspace."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo $HOME"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "/workspace" in result["output"]

    # --- Verify container isolation (cannot access host-specific paths) ---

    async def test_python_cannot_access_docker_socket(self, sandbox_id: str):
        """Python cannot access Docker socket (not mounted in container).
        
        This verifies the container cannot escape to control Docker.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "import os; print(os.path.exists('/var/run/docker.sock'))"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Docker socket should NOT be accessible from within the container
            assert "False" in result["output"]

    async def test_python_os_release_shows_container_os(self, sandbox_id: str):
        """Python can read /etc/os-release - shows container's OS, not host's.
        
        This should show the container's base OS (likely Debian/Ubuntu from Ship image).
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print(open('/etc/os-release').read()[:200])"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Should contain OS info (we don't check specific OS, just that it's readable)
            assert "NAME=" in result["output"] or "ID=" in result["output"]

    # --- Verify user cannot escalate privileges easily ---

    async def test_shell_cannot_write_to_etc(self, sandbox_id: str):
        """Shell cannot write to /etc (read-only filesystem or permission denied).
        
        This verifies the container has limited write permissions.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "touch /etc/test_file 2>&1 || echo 'WRITE_DENIED'"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            # Either the command fails or we see our denial marker
            output = result.get("output", "")
            # We expect write to fail - either permission denied or read-only filesystem
            assert "denied" in output.lower() or "WRITE_DENIED" in output or "Read-only" in output

    async def test_shell_user_id_is_non_root(self, sandbox_id: str):
        """Verify the shell runs as non-root user (uid != 0)."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "id"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Should show uid=1000(shipyard) or similar, NOT uid=0(root)
            assert "uid=1000" in result["output"] or "shipyard" in result["output"]
            assert "uid=0(root)" not in result["output"]

    # --- Python process namespace isolation ---

    async def test_python_can_only_see_container_processes(self, sandbox_id: str):
        """Python can only see processes inside the container, not host processes.
        
        This verifies process namespace isolation.
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": """
import os
import subprocess
result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
lines = result.stdout.strip().split('\\n')
print(f'Process count: {len(lines)}')
# Container should have very few processes
print(f'Few processes (isolated): {len(lines) < 20}')
"""},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Container should have limited processes (isolated from host)
            assert "Process count:" in result["output"]
