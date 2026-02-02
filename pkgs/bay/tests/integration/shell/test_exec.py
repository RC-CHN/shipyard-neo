"""Shell execution tests.

Purpose: Verify shell command execution works correctly.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_echo():
    """Simple echo command."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "echo 'hello world'"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert r.json()["success"] is True
            assert "hello world" in r.json()["output"]


async def test_pwd_default_workspace():
    """pwd returns /workspace by default."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "pwd"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "/workspace" in r.json()["output"]


async def test_cwd_relative():
    """Shell with relative cwd works."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            # Create dir
            await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": "subdir/x.txt", "content": "x"},
                timeout=120.0,
            )
            # Run in subdir
            r = await client.post(
                f"/v1/sandboxes/{sid}/shell/exec",
                json={"command": "pwd", "cwd": "subdir"},
                timeout=30.0,
            )
            assert r.status_code == 200
            assert "subdir" in r.json()["output"]


async def test_ls():
    """ls lists files."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": "myfile.txt", "content": "hi"},
                timeout=120.0,
            )
            r = await client.post(
                f"/v1/sandboxes/{sid}/shell/exec",
                json={"command": "ls"},
                timeout=30.0,
            )
            assert r.status_code == 200
            assert "myfile.txt" in r.json()["output"]


async def test_exit_code_nonzero():
    """Non-zero exit code has success=False."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "exit 1"},
                timeout=120.0,
            )
            assert r.status_code == 200
            result = r.json()
            assert result["success"] is False
            assert result["exit_code"] == 1


async def test_command_not_found():
    """Non-existent command fails."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "nonexistent_cmd_12345"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert r.json()["success"] is False


async def test_pipe():
    """Piped commands work."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "echo 'hello' | cat"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "hello" in r.json()["output"]


async def test_multiline_output():
    """Multiline output works."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "echo -e 'line1\\nline2\\nline3'"},
                timeout=120.0,
            )
            assert r.status_code == 200
            output = r.json()["output"]
            assert "line1" in output and "line2" in output and "line3" in output


async def test_env_variables():
    """Environment variables work."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "echo $HOME"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "/workspace" in r.json()["output"]


async def test_file_manipulation():
    """Shell can create files."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            await client.post(
                f"/v1/sandboxes/{sid}/shell/exec",
                json={"command": "echo 'created by shell' > shell.txt"},
                timeout=120.0,
            )
            r = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": "shell.txt"},
                timeout=30.0,
            )
            assert r.status_code == 200
            assert "created by shell" in r.json()["content"]


async def test_whoami():
    """Commands run as shipyard user."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "whoami"},
                timeout=120.0,
            )
            assert r.status_code == 200
            assert "shipyard" in r.json()["output"]


async def test_isolation():
    """Shell is isolated from host."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            r = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/shell/exec",
                json={"command": "cat /etc/passwd"},
                timeout=120.0,
            )
            assert r.status_code == 200
            # Container passwd should have shipyard user
            if r.json()["success"]:
                assert "shipyard" in r.json()["output"]
