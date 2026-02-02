"""Text file operations tests: read/write/list/delete.

Purpose: Verify filesystem API for text files.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_write_and_read():
    """Write file and read it back."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            content = "Hello E2E!\nLine 2\nLine 3"
            path = "test.txt"

            # Write
            w = await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": path, "content": content},
                timeout=120.0,
            )
            assert w.status_code == 200

            # Read
            r = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": path},
                timeout=30.0,
            )
            assert r.status_code == 200
            assert r.json()["content"] == content


async def test_list_directory():
    """List directory after creating files."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # Write files
            await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": "file1.txt", "content": "a"},
                timeout=120.0,
            )
            await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": "file2.py", "content": "b"},
                timeout=30.0,
            )

            # List
            r = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/directories",
                params={"path": "."},
                timeout=30.0,
            )
            assert r.status_code == 200
            names = [e["name"] for e in r.json().get("entries", [])]
            assert "file1.txt" in names
            assert "file2.py" in names


async def test_delete_file():
    """Delete file and verify it's gone."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            path = "to_delete.txt"

            # Write
            await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": path, "content": "bye"},
                timeout=120.0,
            )

            # Delete
            d = await client.delete(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": path},
                timeout=30.0,
            )
            assert d.status_code == 200

            # Verify gone
            r = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/download",
                params={"path": path},
                timeout=30.0,
            )
            assert r.status_code == 404


async def test_write_nested_path():
    """Write file to nested directory."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]
            path = "a/b/c.txt"
            content = "nested"

            w = await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={"path": path, "content": content},
                timeout=120.0,
            )
            assert w.status_code == 200

            r = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": path},
                timeout=30.0,
            )
            assert r.status_code == 200
            assert r.json()["content"] == content
