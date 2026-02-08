"""Tests for BayClient."""

import re

import pytest

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

    @pytest.mark.asyncio
    async def test_python_exec_returns_execution_metadata(self, httpx_mock, mock_sandbox_response):
        """Python exec should return execution metadata fields when provided by API."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/python/exec",
            json={
                "success": True,
                "output": "hello\\n",
                "error": None,
                "data": {"execution_count": 1, "output": {"text": "hello", "images": []}},
                "execution_id": "exec-123",
                "execution_time_ms": 4,
                "code": "print('hello')",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.python.exec(
                "print('hello')",
                include_code=True,
                description="hello test",
                tags="smoke,python",
            )
            assert result.success is True
            assert result.execution_id == "exec-123"
            assert result.execution_time_ms == 4
            assert result.code == "print('hello')"

    @pytest.mark.asyncio
    async def test_sandbox_execution_history_methods(self, httpx_mock, mock_sandbox_response):
        """Sandbox history methods should map API responses correctly."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://localhost:8000/v1/sandboxes/sbx_123/history.*"),
            json={
                "entries": [
                    {
                        "id": "exec-123",
                        "session_id": "sess-1",
                        "exec_type": "python",
                        "code": "print('hello')",
                        "success": True,
                        "execution_time_ms": 5,
                        "output": "hello\\n",
                        "error": None,
                        "description": "hello",
                        "tags": "demo,python",
                        "notes": None,
                        "created_at": "2026-02-08T00:00:00Z",
                    }
                ],
                "total": 1,
            },
            status_code=200,
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/sandboxes/sbx_123/history/exec-123",
            json={
                "id": "exec-123",
                "session_id": "sess-1",
                "exec_type": "python",
                "code": "print('hello')",
                "success": True,
                "execution_time_ms": 5,
                "output": "hello\\n",
                "error": None,
                "description": "hello",
                "tags": "demo,python",
                "notes": None,
                "created_at": "2026-02-08T00:00:00Z",
            },
            status_code=200,
        )
        httpx_mock.add_response(
            method="PATCH",
            url="http://localhost:8000/v1/sandboxes/sbx_123/history/exec-123",
            json={
                "id": "exec-123",
                "session_id": "sess-1",
                "exec_type": "python",
                "code": "print('hello')",
                "success": True,
                "execution_time_ms": 5,
                "output": "hello\\n",
                "error": None,
                "description": "updated",
                "tags": "demo,python",
                "notes": "reusable",
                "created_at": "2026-02-08T00:00:00Z",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            history = await sandbox.get_execution_history(success_only=True, limit=10)
            assert history.total == 1
            assert history.entries[0].id == "exec-123"

            entry = await sandbox.get_execution("exec-123")
            assert entry.exec_type == "python"

            updated = await sandbox.annotate_execution(
                "exec-123",
                description="updated",
                notes="reusable",
            )
            assert updated.description == "updated"
            assert updated.notes == "reusable"

    @pytest.mark.asyncio
    async def test_skill_manager_lifecycle(self, httpx_mock):
        """Client skills manager should parse candidate/evaluation/release payloads."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/skills/candidates",
            json={
                "id": "sc-1",
                "skill_key": "csv-loader",
                "scenario_key": "etl",
                "payload_ref": None,
                "source_execution_ids": ["exec-1"],
                "status": "draft",
                "latest_score": None,
                "latest_pass": None,
                "last_evaluated_at": None,
                "promotion_release_id": None,
                "created_by": "default",
                "created_at": "2026-02-08T00:00:00Z",
                "updated_at": "2026-02-08T00:00:00Z",
            },
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/skills/candidates/sc-1/evaluate",
            json={
                "id": "se-1",
                "candidate_id": "sc-1",
                "benchmark_id": "bench-1",
                "score": 0.95,
                "passed": True,
                "report": "ok",
                "evaluated_by": "default",
                "created_at": "2026-02-08T00:01:00Z",
            },
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/skills/candidates/sc-1/promote",
            json={
                "id": "sr-1",
                "skill_key": "csv-loader",
                "candidate_id": "sc-1",
                "version": 1,
                "stage": "canary",
                "is_active": True,
                "promoted_by": "default",
                "promoted_at": "2026-02-08T00:02:00Z",
                "rollback_of": None,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            candidate = await client.skills.create_candidate(
                skill_key="csv-loader",
                source_execution_ids=["exec-1"],
                scenario_key="etl",
            )
            assert candidate.status.value == "draft"

            evaluation = await client.skills.evaluate_candidate(
                "sc-1",
                passed=True,
                score=0.95,
                benchmark_id="bench-1",
                report="ok",
            )
            assert evaluation.passed is True

            release = await client.skills.promote_candidate("sc-1")
            assert release.version == 1
            assert release.stage.value == "canary"
