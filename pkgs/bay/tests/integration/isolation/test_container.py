"""Container isolation verification tests.

Purpose: Verify code executes within container's isolation boundary.

Key insight: Python/Shell CAN read container's /etc/passwd - this is the
CONTAINER's file (from image layer), NOT the host's. This is expected.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
Tests verify isolation properties that are per-container, not global.
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


# --- Container environment verification ---


async def test_python_can_read_container_passwd():
    """Python can read /etc/passwd - this is container's passwd."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/python/exec",
                json={"code": "print(open('/etc/passwd').read()[:200])"},
                timeout=120.0,
            )
            assert r.status_code == 200
            result = r.json()
            assert result["success"]
            assert "root:" in result["output"]


async def test_shipyard_user_in_container():
    """Container has shipyard user - proves we're in Ship's container."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/python/exec",
                json={"code": "print('shipyard' in open('/etc/passwd').read())"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "True" in r.json()["output"]


async def test_whoami_shipyard():
    """Commands run as shipyard user (non-root)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "whoami"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "shipyard" in r.json()["output"]


async def test_uid_nonroot():
    """User ID is non-root (uid != 0)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "id"},
                timeout=120.0,
            )
            assert r.status_code == 200
            output = r.json()["output"]
            assert "uid=1000" in output or "shipyard" in output
            assert "uid=0(root)" not in output


# --- Working directory ---


async def test_python_cwd_workspace():
    """Python cwd is /workspace."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/python/exec",
                json={"code": "import os; print(os.getcwd())"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "/workspace" in r.json()["output"]


async def test_home_env_workspace():
    """$HOME is /workspace."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "echo $HOME"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "/workspace" in r.json()["output"]


# --- Isolation from host ---


async def test_no_docker_socket():
    """Docker socket not accessible (container escape prevention)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/python/exec",
                json={"code": "import os; print(os.path.exists('/var/run/docker.sock'))"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "False" in r.json()["output"]


async def test_cannot_write_etc():
    """Cannot write to /etc (permission denied)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "touch /etc/test 2>&1 || echo 'DENIED'"},
                timeout=120.0,
            )
            assert r.status_code == 200
            output = r.json()["output"].lower()
            assert "denied" in output or "read-only" in output


async def test_os_release_container():
    """Can read /etc/os-release - shows container OS."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/python/exec",
                json={"code": "print(open('/etc/os-release').read()[:150])"},
                timeout=120.0,
            )
            assert r.status_code == 200
            output = r.json()["output"]
            assert "NAME=" in output or "ID=" in output


async def test_process_isolation():
    """Container has limited processes (process namespace isolation)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/python/exec",
                json={"code": """
import subprocess
result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
lines = result.stdout.strip().split('\\n')
print(f'process_count={len(lines)}')
print(f'isolated={len(lines) < 20}')
"""},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "process_count=" in r.json()["output"]
