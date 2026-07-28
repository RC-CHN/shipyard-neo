"""Standalone SDK regression coverage for Shipyard Neo issue #21."""

from __future__ import annotations

import re

import pytest

from shipyard_neo import BayClient


@pytest.mark.asyncio
async def test_download_preserves_unicode_absolute_path_and_uses_utf8_query_encoding(
    httpx_mock,
) -> None:
    """A Unicode /workspace path is passed to Bay intact, not stripped or quoted twice."""
    remote_path = "/workspace/数字1到10 🧪.xlsx"
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8000/v1/sandboxes",
        json={
            "id": "sbx_123",
            "status": "ready",
            "profile": "python-default",
            "cargo_id": "cargo_456",
            "capabilities": ["filesystem"],
            "created_at": "2026-02-06T00:00:00Z",
            "expires_at": "2026-02-06T01:00:00Z",
            "idle_expires_at": "2026-02-06T00:05:00Z",
        },
        status_code=201,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(r"http://localhost:8000/v1/sandboxes/sbx_123/filesystem/download.*"),
        content=b"workbook",
    )

    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="test-token",
    ) as client:
        sandbox = await client.create_sandbox()
        content = await sandbox.filesystem.download(remote_path)

    assert content == b"workbook"
    download_request = next(
        request
        for request in httpx_mock.get_requests()
        if request.url.path.endswith("/filesystem/download")
    )
    assert download_request.url.params["path"] == remote_path
    raw_url = str(download_request.url)
    assert "%E6%95%B0%E5%AD%971%E5%88%B010" in raw_url
    assert "%F0%9F%A7%AA" in raw_url
