"""Tests for BayClient."""

import pytest
from httpx import Response

from shipyard_neo import BayClient
from shipyard_neo.errors import NotFoundError


@pytest.fixture
def mock_sandbox_response():
    """Sample sandbox response."""
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


class TestBayClient:
    """Tests for BayClient initialization and context management."""

    def test_requires_endpoint_url(self, monkeypatch):
        """Should raise if no endpoint_url provided."""
        monkeypatch.delenv("BAY_ENDPOINT", raising=False)
        monkeypatch.delenv("BAY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="endpoint_url required"):
            BayClient(access_token="test-token")

    def test_requires_access_token(self, monkeypatch):
        """Should raise if no access_token provided."""
        monkeypatch.delenv("BAY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="access_token required"):
            BayClient(endpoint_url="http://localhost:8000")

    def test_uses_env_vars(self, monkeypatch):
        """Should use environment variables as fallback."""
        monkeypatch.setenv("BAY_ENDPOINT", "http://env-endpoint:8000")
        monkeypatch.setenv("BAY_TOKEN", "env-token")

        client = BayClient()
        assert client._endpoint_url == "http://env-endpoint:8000"
        assert client._access_token == "env-token"

    @pytest.mark.asyncio
    async def test_context_manager(self, httpx_mock, mock_sandbox_response):
        """Should properly initialize and cleanup HTTP client."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            assert sandbox.id == "sbx_123"

    @pytest.mark.asyncio
    async def test_not_found_error(self, httpx_mock):
        """Should raise NotFoundError on 404."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/sandboxes/nonexistent",
            json={"error": {"code": "not_found", "message": "Sandbox not found"}},
            status_code=404,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            with pytest.raises(NotFoundError):
                await client.get_sandbox("nonexistent")

    @pytest.mark.asyncio
    async def test_list_sandboxes_pagination(self, httpx_mock):
        """Should handle pagination correctly."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/sandboxes?limit=10",
            json={
                "items": [
                    {
                        "id": "sbx_1",
                        "status": "ready",
                        "profile": "python-default",
                        "cargo_id": "cargo_1",
                        "capabilities": ["python"],
                        "created_at": "2026-02-06T00:00:00Z",
                        "expires_at": None,
                        "idle_expires_at": None,
                    }
                ],
                "next_cursor": "cursor_abc",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            result = await client.list_sandboxes(limit=10)
            assert len(result.items) == 1
            assert result.items[0].id == "sbx_1"
            assert result.next_cursor == "cursor_abc"
