"""Capability enforcement tests.

Purpose: Verify profile-level capability enforcement blocks unauthorized operations.

Parallel-safe: Yes - each test creates/deletes its own sandbox.

Note: Requires python-only-test profile configured in Bay:
  profiles:
    - id: python-only-test
      capabilities: [python]
"""

from __future__ import annotations

import httpx
import pytest

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks

RESTRICTED_PROFILE = "python-only-test"


@pytest.fixture
async def restricted_sandbox_id():
    """Create sandbox with restricted profile."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=60.0
    ) as client:
        r = await client.post("/v1/sandboxes", json={"profile": RESTRICTED_PROFILE})
        if r.status_code == 400 and "profile" in r.text.lower():
            pytest.skip(f"Profile '{RESTRICTED_PROFILE}' not configured")
        assert r.status_code == 201
        sid = r.json()["id"]
        yield sid
        try:
            await client.delete(f"/v1/sandboxes/{sid}", timeout=120.0)
        except httpx.TimeoutException:
            pass  # Cleanup will be handled by GC or cleanup script


@pytest.fixture
async def full_sandbox_id():
    """Create sandbox with full profile."""
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


# --- Restricted profile tests ---


async def test_allowed_capability_succeeds(restricted_sandbox_id: str):
    """Python execution succeeds on python-only profile."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{restricted_sandbox_id}/python/exec",
            json={"code": "print('ok')"},
            timeout=60.0,
        )
        assert r.status_code == 200
        assert r.json()["success"]


@pytest.mark.parametrize(
    "endpoint,method,params_or_json",
    [
        ("/shell/exec", "POST", {"command": "echo hi"}),
        ("/filesystem/files", "GET", {"path": "x.txt"}),
        ("/filesystem/files", "PUT", {"path": "x.txt", "content": "x"}),
        ("/filesystem/directories", "GET", {"path": "."}),
        ("/filesystem/files", "DELETE", {"path": "x.txt"}),
        ("/filesystem/download", "GET", {"path": "x.txt"}),
    ],
    ids=["shell", "fs_read", "fs_write", "fs_list", "fs_delete", "fs_download"],
)
async def test_capability_denied(
    restricted_sandbox_id: str, endpoint: str, method: str, params_or_json: dict
):
    """Capabilities not in profile are blocked with 400."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        url = f"/v1/sandboxes/{restricted_sandbox_id}{endpoint}"
        if method == "GET":
            r = await client.get(url, params=params_or_json, timeout=10.0)
        elif method == "PUT":
            r = await client.put(url, json=params_or_json, timeout=10.0)
        elif method == "DELETE":
            r = await client.delete(url, params=params_or_json, timeout=10.0)
        else:
            r = await client.post(url, json=params_or_json, timeout=10.0)

        assert r.status_code == 400
        assert r.json()["error"]["code"] == "capability_not_supported"


async def test_upload_denied(restricted_sandbox_id: str):
    """Upload blocked on restricted profile."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{restricted_sandbox_id}/filesystem/upload",
            files={"file": ("x.txt", b"x", "text/plain")},
            data={"path": "x.txt"},
            timeout=10.0,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "capability_not_supported"


# --- Full profile tests ---


async def test_full_profile_python(full_sandbox_id: str):
    """Python works on full profile."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{full_sandbox_id}/python/exec",
            json={"code": "1+1"},
            timeout=60.0,
        )
        assert r.status_code == 200


async def test_full_profile_shell(full_sandbox_id: str):
    """Shell works on full profile."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r = await client.post(
            f"/v1/sandboxes/{full_sandbox_id}/shell/exec",
            json={"command": "echo ok"},
            timeout=60.0,
        )
        assert r.status_code == 200


async def test_full_profile_filesystem(full_sandbox_id: str):
    """Filesystem works on full profile."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Ensure session started
        await client.post(
            f"/v1/sandboxes/{full_sandbox_id}/python/exec",
            json={"code": "1"},
            timeout=60.0,
        )
        r = await client.get(
            f"/v1/sandboxes/{full_sandbox_id}/filesystem/directories",
            params={"path": "."},
        )
        assert r.status_code == 200
