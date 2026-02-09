"""Unit tests for GullAdapter.

These tests validate:
- HTTP wiring (/meta, /exec)
- RuntimeMeta parsing + caching
- ExecutionResult mapping

Note: We use httpx.MockTransport + monkeypatch to provide a shared client.
"""

from __future__ import annotations

import pytest
import httpx

import app.adapters.gull as gull_mod
from app.adapters.gull import GullAdapter
from app.errors import RequestTimeoutError, ShipError


@pytest.fixture
async def mock_client(monkeypatch: pytest.MonkeyPatch):
    """Provide a shared httpx.AsyncClient via _get_shared_client()."""

    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))

        if request.url.path == "/meta":
            return httpx.Response(
                200,
                json={
                    "runtime": {"name": "gull", "version": "0.1.0", "api_version": "v1"},
                    "workspace": {"mount_path": "/workspace"},
                    "capabilities": {"browser": {"version": "1.0"}},
                },
            )

        if request.url.path == "/exec":
            # httpx.Request does not provide request.json(); parse manually.
            import json as _json

            body = _json.loads(request.content.decode("utf-8")) if request.content else {}
            assert "cmd" in body
            return httpx.Response(
                200,
                json={"stdout": "ok\n", "stderr": "", "exit_code": 0},
            )

        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://gull")

    monkeypatch.setattr(gull_mod, "_get_shared_client", lambda: client)

    yield client, calls

    await client.aclose()


@pytest.mark.asyncio
async def test_supported_capabilities():
    adapter = GullAdapter("http://gull")
    assert adapter.supported_capabilities() == ["browser"]


@pytest.mark.asyncio
async def test_get_meta_parses_and_caches(mock_client):
    _client, calls = mock_client

    adapter = GullAdapter("http://gull")

    meta1 = await adapter.get_meta()
    assert meta1.name == "gull"
    assert meta1.version == "0.1.0"
    assert meta1.api_version == "v1"
    assert meta1.mount_path == "/workspace"
    assert "browser" in meta1.capabilities

    meta2 = await adapter.get_meta()
    assert meta2 is meta1

    # /meta should be called only once due to caching
    assert calls.count(("GET", "/meta")) == 1


@pytest.mark.asyncio
async def test_exec_browser_maps_execution_result(mock_client):
    _client, calls = mock_client

    adapter = GullAdapter("http://gull")
    result = await adapter.exec_browser("open https://example.com", timeout=12)

    assert result.success is True
    assert result.output == "ok\n"
    assert result.error is None
    assert result.exit_code == 0

    assert ("POST", "/exec") in calls


@pytest.mark.asyncio
async def test_http_4xx_maps_to_ship_error(monkeypatch: pytest.MonkeyPatch):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gull")
    monkeypatch.setattr(gull_mod, "_get_shared_client", lambda: client)

    adapter = GullAdapter("http://gull")
    with pytest.raises(ShipError):
        await adapter.get_meta()

    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_maps_to_request_timeout(monkeypatch: pytest.MonkeyPatch):
    class _TimeoutClient:
        async def request(self, *args, **kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(gull_mod, "_get_shared_client", lambda: _TimeoutClient())

    adapter = GullAdapter("http://gull")
    with pytest.raises(RequestTimeoutError):
        await adapter.get_meta()
