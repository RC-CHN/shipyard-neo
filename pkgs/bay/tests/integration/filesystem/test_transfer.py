"""Binary file upload/download tests.

Purpose: Verify filesystem API for binary files.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_upload_and_download_text():
    """Upload text file and download it back."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            content = b"Hello, World!\nLine 2"
            path = "upload.txt"

            # Upload
            u = await client.post(
                f"/v1/sandboxes/{sid}/filesystem/upload",
                files={"file": ("upload.txt", content, "text/plain")},
                data={"path": path},
                timeout=120.0,
            )
            assert u.status_code == 200
            assert u.json()["status"] == "ok"
            assert u.json()["size"] == len(content)

            # Download
            d = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/download",
                params={"path": path},
                timeout=30.0,
            )
            assert d.status_code == 200
            assert d.content == content


async def test_upload_and_download_binary():
    """Upload binary file and download it back."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            # PNG header + random bytes
            content = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(range(256))
            path = "test.bin"

            u = await client.post(
                f"/v1/sandboxes/{sid}/filesystem/upload",
                files={"file": ("test.bin", content, "application/octet-stream")},
                data={"path": path},
                timeout=120.0,
            )
            assert u.status_code == 200

            d = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/download",
                params={"path": path},
                timeout=30.0,
            )
            assert d.status_code == 200
            assert d.content == content


async def test_upload_to_nested_path():
    """Upload file to nested directory."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            content = b"nested"
            path = "a/b/file.txt"

            u = await client.post(
                f"/v1/sandboxes/{sid}/filesystem/upload",
                files={"file": ("file.txt", content, "text/plain")},
                data={"path": path},
                timeout=120.0,
            )
            assert u.status_code == 200

            d = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/download",
                params={"path": path},
                timeout=30.0,
            )
            assert d.status_code == 200
            assert d.content == content


async def test_download_nonexistent_returns_404():
    """Download non-existent file returns 404."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            d = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/download",
                params={"path": "nonexistent.txt"},
                timeout=120.0,  # First access triggers session creation
            )
            assert d.status_code == 404
            assert d.json()["error"]["code"] == "file_not_found"
