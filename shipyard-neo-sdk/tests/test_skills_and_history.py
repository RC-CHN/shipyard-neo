"""SDK tests for execution history and skill lifecycle APIs."""

from __future__ import annotations

import json
import re

import pytest

from shipyard_neo import BayClient
from shipyard_neo.types import SkillCandidateStatus, SkillReleaseStage


@pytest.fixture
def mock_sandbox_response() -> dict[str, object]:
    """Sample sandbox payload used across tests."""
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


class TestSkillsManagerAndHistory:
    def test_skills_property_requires_client_context(self):
        """client.skills should be unavailable outside async context manager."""
        client = BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        )
        with pytest.raises(RuntimeError, match="BayClient not initialized"):
            _ = client.skills

    @pytest.mark.asyncio
    async def test_get_last_execution_supports_exec_type(self, httpx_mock, mock_sandbox_response):
        """Sandbox.get_last_execution should call /history/last with optional exec_type."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/sandboxes/sbx_123/history/last?exec_type=python",
            json={
                "id": "exec-last",
                "session_id": "sess-last",
                "exec_type": "python",
                "code": "print('latest')",
                "success": True,
                "execution_time_ms": 7,
                "output": "latest\n",
                "error": None,
                "description": None,
                "tags": "latest,python",
                "notes": None,
                "created_at": "2026-02-08T00:00:00Z",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            latest = await sandbox.get_last_execution(exec_type="python")
            assert latest.id == "exec-last"
            assert latest.exec_type == "python"
            assert latest.tags == "latest,python"

    @pytest.mark.asyncio
    async def test_get_execution_history_sends_filters(self, httpx_mock, mock_sandbox_response):
        """History list should include advanced filter query params."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="GET",
            url=re.compile(r"http://localhost:8000/v1/sandboxes/sbx_123/history.*"),
            json={"entries": [], "total": 0},
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            history = await sandbox.get_execution_history(
                exec_type="shell",
                success_only=True,
                limit=20,
                offset=5,
                tags="ops",
                has_notes=True,
                has_description=True,
            )
            assert history.total == 0

        request = httpx_mock.get_requests()[-1]
        params = dict(request.url.params)
        assert params["exec_type"] == "shell"
        assert params["success_only"] == "true"
        assert params["limit"] == "20"
        assert params["offset"] == "5"
        assert params["tags"] == "ops"
        assert params["has_notes"] == "true"
        assert params["has_description"] == "true"

    @pytest.mark.asyncio
    async def test_python_exec_forwards_history_metadata_fields(self, httpx_mock, mock_sandbox_response):
        """Python capability should send include_code/description/tags in request body."""
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
                "output": "ok\n",
                "error": None,
                "data": None,
                "execution_id": "exec-meta-py",
                "execution_time_ms": 4,
                "code": "print('ok')",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.python.exec(
                "print('ok')",
                include_code=True,
                description="python metadata",
                tags="python,meta",
            )
            assert result.execution_id == "exec-meta-py"
            assert result.code == "print('ok')"

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["include_code"] is True
        assert body["description"] == "python metadata"
        assert body["tags"] == "python,meta"

    @pytest.mark.asyncio
    async def test_shell_exec_forwards_history_metadata_fields(self, httpx_mock, mock_sandbox_response):
        """Shell capability should send include_code/description/tags/cwd in request body."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes",
            json=mock_sandbox_response,
            status_code=201,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/sandboxes/sbx_123/shell/exec",
            json={
                "success": True,
                "output": "done\n",
                "error": None,
                "exit_code": 0,
                "execution_id": "exec-meta-sh",
                "execution_time_ms": 6,
                "command": "echo done",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            result = await sandbox.shell.exec(
                "echo done",
                cwd="tmp",
                include_code=True,
                description="shell metadata",
                tags="shell,meta",
            )
            assert result.execution_id == "exec-meta-sh"
            assert result.command == "echo done"

        request = httpx_mock.get_requests()[-1]
        body = json.loads(request.content.decode("utf-8"))
        assert body["cwd"] == "tmp"
        assert body["include_code"] is True
        assert body["description"] == "shell metadata"
        assert body["tags"] == "shell,meta"

    @pytest.mark.asyncio
    async def test_skills_list_candidates_serializes_status_enum(self, httpx_mock):
        """skills.list_candidates should serialize enum values in query params."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/candidates?status=draft&limit=5&offset=0",
            json={"items": [], "total": 0},
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            result = await client.skills.list_candidates(
                status=SkillCandidateStatus.DRAFT,
                limit=5,
                offset=0,
            )
            assert result.total == 0

    @pytest.mark.asyncio
    async def test_skills_list_releases_serializes_stage_enum(self, httpx_mock):
        """skills.list_releases should serialize enum stage in query params."""
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:8000/v1/skills/releases?"
                "skill_key=csv-loader&active_only=true&stage=stable&limit=10&offset=0"
            ),
            json={"items": [], "total": 0},
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            releases = await client.skills.list_releases(
                skill_key="csv-loader",
                active_only=True,
                stage=SkillReleaseStage.STABLE,
                limit=10,
                offset=0,
            )
            assert releases.total == 0

    @pytest.mark.asyncio
    async def test_skills_get_candidate_and_rollback_release(self, httpx_mock):
        """skills.get_candidate and skills.rollback_release should parse payloads."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/candidates/sc-1",
            json={
                "id": "sc-1",
                "skill_key": "csv-loader",
                "scenario_key": "etl",
                "payload_ref": None,
                "source_execution_ids": ["exec-1"],
                "status": "promoted",
                "latest_score": 0.98,
                "latest_pass": True,
                "last_evaluated_at": "2026-02-08T00:00:00Z",
                "promotion_release_id": "sr-1",
                "created_by": "default",
                "created_at": "2026-02-08T00:00:00Z",
                "updated_at": "2026-02-08T00:00:01Z",
            },
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/skills/releases/sr-2/rollback",
            json={
                "id": "sr-3",
                "skill_key": "csv-loader",
                "candidate_id": "sc-1",
                "version": 3,
                "stage": "stable",
                "is_active": True,
                "promoted_by": "default",
                "promoted_at": "2026-02-08T00:01:00Z",
                "rollback_of": "sr-2",
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            candidate = await client.skills.get_candidate("sc-1")
            assert candidate.id == "sc-1"
            assert candidate.status.value == "promoted"
            assert candidate.promotion_release_id == "sr-1"

            rollback = await client.skills.rollback_release("sr-2")
            assert rollback.id == "sr-3"
            assert rollback.rollback_of == "sr-2"
            assert rollback.stage.value == "stable"
