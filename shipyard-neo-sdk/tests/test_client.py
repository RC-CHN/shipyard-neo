"""Tests for BayClient."""

import json
import re

import pytest
from pydantic import ValidationError

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

    @pytest.mark.asyncio
    async def test_browser_exec(self, httpx_mock, mock_sandbox_response):
        """Browser exec should return BrowserExecResult."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec",
            json={
                "success": True,
                "output": "Page loaded: https://example.com",
                "error": None,
                "exit_code": 0,
                "execution_id": "exec-browser-1",
                "execution_time_ms": 18,
                "trace_ref": "blob:trace-1",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec("goto https://example.com")
            assert result.success is True
            assert result.output == "Page loaded: https://example.com"
            assert result.exit_code == 0
            assert result.error is None
            assert result.execution_id == "exec-browser-1"
            assert result.execution_time_ms == 18
            assert result.trace_ref == "blob:trace-1"

    @pytest.mark.asyncio
    async def test_browser_exec_with_timeout(self, httpx_mock, mock_sandbox_response):
        """Browser exec should support custom timeout."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec",
            json={
                "success": False,
                "output": "",
                "error": "Navigation timeout exceeded",
                "exit_code": 1,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec(
                "goto https://slow-site.example.com",
                timeout=60,
            )
            assert result.success is False
            assert result.error == "Navigation timeout exceeded"

    @pytest.mark.asyncio
    async def test_browser_exec_forwards_learning_fields(self, httpx_mock, mock_sandbox_response):
        """Browser exec should forward description/tags/learn/include_trace."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec",
            json={
                "success": True,
                "output": "ok",
                "error": None,
                "exit_code": 0,
                "execution_id": "exec-browser-2",
                "execution_time_ms": 9,
                "trace_ref": "blob:trace-2",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec(
                "snapshot -i",
                description="capture controls",
                tags="browser,trace",
                learn=True,
                include_trace=True,
            )
            assert result.execution_id == "exec-browser-2"
            assert result.trace_ref == "blob:trace-2"

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["description"] == "capture controls"
        assert body["tags"] == "browser,trace"
        assert body["learn"] is True
        assert body["include_trace"] is True

    @pytest.mark.asyncio
    async def test_browser_exec_sends_default_learning_flags(
        self, httpx_mock, mock_sandbox_response
    ):
        """Browser exec should keep explicit default flags in request body."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec",
            json={
                "success": True,
                "output": "ok",
                "error": None,
                "exit_code": 0,
                "execution_id": "exec-browser-defaults",
                "execution_time_ms": 8,
                "trace_ref": None,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec("snapshot -i")
            assert result.execution_id == "exec-browser-defaults"
            assert result.trace_ref is None

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["learn"] is False
        assert body["include_trace"] is False

    @pytest.mark.asyncio
    async def test_list_profiles(self, httpx_mock):
        """list_profiles should return ProfileList."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/profiles",
            json={
                "items": [
                    {
                        "id": "python-default",
                        "image": "shipyard/python:3.12",
                        "resources": {"cpus": 1.0, "memory": "512m"},
                        "capabilities": ["python", "shell", "filesystem"],
                        "idle_timeout": 300,
                    },
                    {
                        "id": "browser-enabled",
                        "image": "shipyard/browser:latest",
                        "resources": {"cpus": 2.0, "memory": "2048m"},
                        "capabilities": ["python", "shell", "filesystem", "browser"],
                        "idle_timeout": 600,
                    },
                ]
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            profiles = await client.list_profiles()
            assert len(profiles.items) == 2
            assert profiles.items[0].id == "python-default"
            assert profiles.items[0].capabilities == ["python", "shell", "filesystem"]
            assert profiles.items[0].idle_timeout == 300
            assert profiles.items[0].resources == {"cpus": 1.0, "memory": "512m"}
            assert profiles.items[1].id == "browser-enabled"
            assert "browser" in profiles.items[1].capabilities

    @pytest.mark.asyncio
    async def test_browser_exec_batch_success(self, httpx_mock, mock_sandbox_response):
        """Browser exec_batch should return BrowserBatchExecResult with per-step results."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec_batch",
            json={
                "results": [
                    {
                        "cmd": "open https://example.com",
                        "stdout": "Page loaded\n",
                        "stderr": "",
                        "exit_code": 0,
                        "step_index": 0,
                        "duration_ms": 120,
                    },
                    {
                        "cmd": "wait --load networkidle",
                        "stdout": "Ready\n",
                        "stderr": "",
                        "exit_code": 0,
                        "step_index": 1,
                        "duration_ms": 80,
                    },
                    {
                        "cmd": "snapshot -i",
                        "stdout": "[snapshot content]\n",
                        "stderr": "",
                        "exit_code": 0,
                        "step_index": 2,
                        "duration_ms": 50,
                    },
                ],
                "total_steps": 3,
                "completed_steps": 3,
                "success": True,
                "duration_ms": 250,
                "execution_id": "exec-browser-batch-1",
                "execution_time_ms": 255,
                "trace_ref": "blob:trace-batch-1",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec_batch(
                [
                    "open https://example.com",
                    "wait --load networkidle",
                    "snapshot -i",
                ],
                timeout=120,
            )
            assert result.success is True
            assert result.total_steps == 3
            assert result.completed_steps == 3
            assert result.duration_ms == 250
            assert result.execution_id == "exec-browser-batch-1"
            assert result.execution_time_ms == 255
            assert result.trace_ref == "blob:trace-batch-1"
            assert len(result.results) == 3
            assert result.results[0].cmd == "open https://example.com"
            assert result.results[0].exit_code == 0
            assert result.results[0].duration_ms == 120
            assert result.results[2].cmd == "snapshot -i"

    @pytest.mark.asyncio
    async def test_browser_exec_batch_partial_failure(self, httpx_mock, mock_sandbox_response):
        """Browser exec_batch should handle partial failures (stop_on_error)."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec_batch",
            json={
                "results": [
                    {
                        "cmd": "open https://example.com",
                        "stdout": "Page loaded\n",
                        "stderr": "",
                        "exit_code": 0,
                        "step_index": 0,
                        "duration_ms": 100,
                    },
                    {
                        "cmd": "click @e99",
                        "stdout": "",
                        "stderr": "Element not found: @e99",
                        "exit_code": 1,
                        "step_index": 1,
                        "duration_ms": 30,
                    },
                ],
                "total_steps": 3,
                "completed_steps": 2,
                "success": False,
                "duration_ms": 130,
                "execution_id": "exec-browser-batch-2",
                "execution_time_ms": 136,
                "trace_ref": None,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec_batch(
                ["open https://example.com", "click @e99", "snapshot -i"],
                stop_on_error=True,
            )
            assert result.success is False
            assert result.total_steps == 3
            assert result.completed_steps == 2
            assert result.execution_id == "exec-browser-batch-2"
            assert len(result.results) == 2
            assert result.results[1].exit_code == 1
            assert result.results[1].stderr == "Element not found: @e99"

    @pytest.mark.asyncio
    async def test_browser_exec_batch_forwards_learning_fields(
        self, httpx_mock, mock_sandbox_response
    ):
        """Browser exec_batch should forward metadata and learning flags."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/exec_batch",
            json={
                "results": [
                    {
                        "cmd": "open about:blank",
                        "stdout": "ok\n",
                        "stderr": "",
                        "exit_code": 0,
                        "step_index": 0,
                        "duration_ms": 15,
                    }
                ],
                "total_steps": 1,
                "completed_steps": 1,
                "success": True,
                "duration_ms": 15,
                "execution_id": "exec-browser-batch-meta",
                "execution_time_ms": 17,
                "trace_ref": "blob:trace-batch-meta",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.exec_batch(
                ["open about:blank"],
                description="batch metadata",
                tags="browser,batch,meta",
                learn=True,
                include_trace=True,
            )
            assert result.execution_id == "exec-browser-batch-meta"
            assert result.trace_ref == "blob:trace-batch-meta"

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["description"] == "batch metadata"
        assert body["tags"] == "browser,batch,meta"
        assert body["learn"] is True
        assert body["include_trace"] is True

    @pytest.mark.asyncio
    async def test_browser_run_skill(self, httpx_mock, mock_sandbox_response):
        """Browser run_skill should return replay metadata and steps."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/skills/login-flow/run",
            json={
                "skill_key": "login-flow",
                "release_id": "sr-1",
                "execution_id": "exec-run-1",
                "execution_time_ms": 88,
                "trace_ref": "blob:trace-run-1",
                "results": [
                    {
                        "cmd": "open https://example.com/login",
                        "stdout": "ok",
                        "stderr": "",
                        "exit_code": 0,
                        "step_index": 0,
                        "duration_ms": 40,
                    }
                ],
                "total_steps": 1,
                "completed_steps": 1,
                "success": True,
                "duration_ms": 40,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.run_skill(
                "login-flow",
                include_trace=True,
                tags="replay",
            )
            assert result.skill_key == "login-flow"
            assert result.release_id == "sr-1"
            assert result.execution_id == "exec-run-1"
            assert result.trace_ref == "blob:trace-run-1"
            assert result.success is True

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["include_trace"] is True
        assert body["tags"] == "replay"

    @pytest.mark.asyncio
    async def test_browser_run_skill_forwards_timeout_and_description(
        self, httpx_mock, mock_sandbox_response
    ):
        """Browser run_skill should send timeout/stop_on_error/description/tags."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/skills/checkout/run",
            json={
                "skill_key": "checkout",
                "release_id": "sr-checkout",
                "execution_id": "exec-run-checkout",
                "execution_time_ms": 45,
                "trace_ref": None,
                "results": [],
                "total_steps": 0,
                "completed_steps": 0,
                "success": True,
                "duration_ms": 0,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.run_skill(
                "checkout",
                timeout=150,
                stop_on_error=False,
                include_trace=False,
                description="checkout replay",
                tags="skill:checkout,replay",
            )
            assert result.execution_id == "exec-run-checkout"
            assert result.trace_ref is None

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["timeout"] == 150
        assert body["stop_on_error"] is False
        assert body["include_trace"] is False
        assert body["description"] == "checkout replay"
        assert body["tags"] == "skill:checkout,replay"

    @pytest.mark.asyncio
    async def test_browser_run_skill_sends_default_flags(self, httpx_mock, mock_sandbox_response):
        """Browser run_skill should send default timeout/stop_on_error/include_trace."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/browser/skills/login/run",
            json={
                "skill_key": "login",
                "release_id": "sr-login",
                "execution_id": "exec-run-login",
                "execution_time_ms": 35,
                "trace_ref": None,
                "results": [],
                "total_steps": 0,
                "completed_steps": 0,
                "success": True,
                "duration_ms": 0,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.browser.run_skill("login")
            assert result.execution_id == "exec-run-login"

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["timeout"] == 60
        assert body["stop_on_error"] is True
        assert body["include_trace"] is False

    @pytest.mark.asyncio
    async def test_browser_exec_batch_rejects_empty_commands_before_http(
        self, httpx_mock, mock_sandbox_response
    ):
        """Browser exec_batch should fail fast on empty command list."""
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
            with pytest.raises(ValidationError):
                await sandbox.browser.exec_batch([])

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert str(requests[0].url) == "http://localhost:8000/v1/sandboxes"
