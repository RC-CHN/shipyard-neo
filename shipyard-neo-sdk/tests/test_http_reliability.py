"""Tests for SDK HTTP reliability and error fallback behavior."""

from __future__ import annotations

import pytest

from shipyard_neo import BayClient
from shipyard_neo.errors import BayError, NotFoundError, ShipError


def _sandbox_payload() -> dict[str, object]:
    return {
        "id": "sbx_123",
        "status": "ready",
        "profile": "python-default",
        "cargo_id": "cargo_456",
        "capabilities": ["python", "shell", "filesystem"],
        "created_at": "2026-02-06T00:00:00Z",
        "expires_at": "2026-02-06T01:00:00Z",
        "idle_expires_at": "2026-02-06T00:05:00Z",
    }


@pytest.mark.asyncio
async def test_get_retries_on_transient_503(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8000/v1/sandboxes?limit=10",
        status_code=503,
        json={"error": {"code": "session_not_ready", "message": "warming up"}},
    )
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8000/v1/sandboxes?limit=10",
        status_code=200,
        json={"items": [], "next_cursor": None},
    )

    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="test-token",
        max_retries=1,
    ) as client:
        result = await client.list_sandboxes(limit=10)
        assert result.items == []
        assert result.next_cursor is None

    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_post_without_idempotency_key_is_not_retried(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8000/v1/sandboxes",
        status_code=503,
        json={"error": {"code": "session_not_ready", "message": "warming up"}},
    )

    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="test-token",
        max_retries=3,
    ) as client:
        with pytest.raises(BayError):
            await client.create_sandbox()

    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_post_with_idempotency_key_retries(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8000/v1/sandboxes",
        status_code=503,
        json={"error": {"code": "session_not_ready", "message": "warming up"}},
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8000/v1/sandboxes",
        status_code=201,
        json=_sandbox_payload(),
    )

    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="test-token",
        max_retries=1,
    ) as client:
        sandbox = await client.create_sandbox(idempotency_key="idem-1")
        assert sandbox.id == "sbx_123"

    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_non_json_404_maps_to_not_found(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8000/v1/sandboxes/missing",
        status_code=404,
        text="<html>not found</html>",
    )

    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="test-token",
    ) as client:
        with pytest.raises(NotFoundError) as exc_info:
            await client.get_sandbox("missing")

    assert "non-JSON" in exc_info.value.message
    assert "raw_response_snippet" in exc_info.value.details


@pytest.mark.asyncio
async def test_non_json_502_maps_to_ship_error_with_bounded_snippet(httpx_mock):
    raw_text = "x" * 600
    httpx_mock.add_response(
        method="GET",
        url="http://localhost:8000/v1/sandboxes/sbx_123",
        status_code=502,
        text=raw_text,
    )

    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="test-token",
        max_retries=0,
    ) as client:
        with pytest.raises(ShipError) as exc_info:
            await client.get_sandbox("sbx_123")

    details = exc_info.value.details
    assert details["raw_response_truncated"] is True
    assert len(details["raw_response_snippet"]) == 500
