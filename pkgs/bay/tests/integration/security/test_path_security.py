"""Path security validation tests.

Purpose: Verify Bay rejects malicious paths (traversal, absolute) with 400.

Parallel-safe: Yes - uses a single shared sandbox fixture per test class.
"""

from __future__ import annotations

import httpx
import pytest

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


@pytest.fixture
async def sandbox_id():
    """Create sandbox for path security tests."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=60.0
    ) as client:
        r = await client.post("/v1/sandboxes", json={"profile": DEFAULT_PROFILE})
        assert r.status_code == 201
        sid = r.json()["id"]
        yield sid
        try:
            await client.delete(f"/v1/sandboxes/{sid}", timeout=120.0)
        except httpx.TimeoutException:
            pass  # Cleanup will be handled by GC or cleanup script


# --- Reject tests (malicious paths) ---


@pytest.mark.parametrize(
    "path,reason",
    [
        ("/etc/passwd", "absolute_path"),
        ("../secret.txt", "path_traversal"),
        ("a/../../etc/passwd", "path_traversal"),
    ],
    ids=["absolute", "traversal", "deep_traversal"],
)
async def test_read_rejects_malicious_path(sandbox_id: str, path: str, reason: str):
    """GET /filesystem/files rejects malicious paths."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.get(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            params={"path": path},
            timeout=30.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_path"


@pytest.mark.parametrize(
    "path",
    ["/tmp/evil.sh", "../secret.txt"],
    ids=["absolute", "traversal"],
)
async def test_write_rejects_malicious_path(sandbox_id: str, path: str):
    """PUT /filesystem/files rejects malicious paths."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.put(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            json={"path": path, "content": "evil"},
            timeout=30.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_path"


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../"],
    ids=["absolute", "traversal"],
)
async def test_list_rejects_malicious_path(sandbox_id: str, path: str):
    """GET /filesystem/directories rejects malicious paths."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.get(
            f"/v1/sandboxes/{sandbox_id}/filesystem/directories",
            params={"path": path},
            timeout=30.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_path"


async def test_delete_rejects_absolute_path(sandbox_id: str):
    """DELETE /filesystem/files rejects absolute paths."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.delete(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            params={"path": "/etc/passwd"},
            timeout=30.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_path"


async def test_download_rejects_absolute_path(sandbox_id: str):
    """GET /filesystem/download rejects absolute paths."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.get(
            f"/v1/sandboxes/{sandbox_id}/filesystem/download",
            params={"path": "/etc/passwd"},
            timeout=30.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_path"


@pytest.mark.parametrize(
    "path",
    ["/tmp/evil.txt", "../../evil.txt"],
    ids=["absolute", "traversal"],
)
async def test_upload_rejects_malicious_path(sandbox_id: str, path: str):
    """POST /filesystem/upload rejects malicious paths."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{sandbox_id}/filesystem/upload",
            files={"file": ("test.txt", b"evil", "text/plain")},
            data={"path": path},
            timeout=30.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_path"


@pytest.mark.parametrize(
    "cwd,reason",
    [("/etc", "absolute_path"), ("../", "path_traversal")],
    ids=["absolute", "traversal"],
)
async def test_shell_rejects_malicious_cwd(sandbox_id: str, cwd: str, reason: str):
    """POST /shell/exec rejects malicious cwd."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{sandbox_id}/shell/exec",
            json={"command": "ls", "cwd": cwd},
            timeout=30.0,
        )
        assert r.status_code == 400
        error = r.json()["error"]
        assert error["code"] == "invalid_path"
        assert error["details"]["field"] == "cwd"


# --- Allow tests (valid paths) ---


async def test_valid_internal_traversal_allowed(sandbox_id: str):
    """Path with internal .. that normalizes safely is allowed."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Write
        await client.put(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            json={"path": "test.txt", "content": "ok"},
            timeout=120.0,
        )
        # Read with internal traversal
        r = await client.get(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            params={"path": "subdir/../test.txt"},
            timeout=30.0,
        )
        assert r.status_code == 200
        assert r.json()["content"] == "ok"


async def test_hidden_file_allowed(sandbox_id: str):
    """Hidden files like .gitignore are allowed."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.put(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            json={"path": ".gitignore", "content": "*.pyc"},
            timeout=120.0,
        )
        assert r.status_code == 200


async def test_shell_cwd_none_uses_workspace(sandbox_id: str):
    """Shell with cwd=None uses /workspace."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{sandbox_id}/shell/exec",
            json={"command": "pwd"},
            timeout=120.0,
        )
        assert r.status_code == 200
        assert "/workspace" in r.json()["output"]


async def test_shell_relative_cwd_allowed(sandbox_id: str):
    """Shell with valid relative cwd works."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Create dir
        await client.put(
            f"/v1/sandboxes/{sandbox_id}/filesystem/files",
            json={"path": "subdir/x.txt", "content": "x"},
            timeout=120.0,
        )
        # Run in subdir
        r = await client.post(
            f"/v1/sandboxes/{sandbox_id}/shell/exec",
            json={"command": "pwd", "cwd": "subdir"},
            timeout=30.0,
        )
        assert r.status_code == 200
        assert "subdir" in r.json()["output"]
