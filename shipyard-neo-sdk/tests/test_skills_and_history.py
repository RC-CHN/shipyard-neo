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
    async def test_history_parses_browser_learning_fields(self, httpx_mock, mock_sandbox_response):
        """History entries should parse payload_ref and browser learning fields."""
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
                        "id": "exec-browser-1",
                        "session_id": "sess-browser",
                        "exec_type": "browser_batch",
                        "code": "open about:blank\nsnapshot -i",
                        "success": True,
                        "execution_time_ms": 12,
                        "output": "ok\n",
                        "error": None,
                        "payload_ref": "blob:trace-123",
                        "description": "browser learn run",
                        "tags": "skill:browser-checkout,trace",
                        "notes": None,
                        "learn_enabled": True,
                        "learn_status": "pending",
                        "learn_error": None,
                        "learn_processed_at": None,
                        "created_at": "2026-02-10T00:00:00Z",
                    }
                ],
                "total": 1,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            sandbox = await client.create_sandbox()
            history = await sandbox.get_execution_history(exec_type="browser_batch")
            assert history.total == 1
            entry = history.entries[0]
            assert entry.exec_type == "browser_batch"
            assert entry.payload_ref == "blob:trace-123"
            assert entry.learn_enabled is True
            assert entry.learn_status == "pending"

    @pytest.mark.asyncio
    async def test_python_exec_forwards_history_metadata_fields(
        self, httpx_mock, mock_sandbox_response
    ):
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
    async def test_shell_exec_forwards_history_metadata_fields(
        self, httpx_mock, mock_sandbox_response
    ):
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

    @pytest.mark.asyncio
    async def test_skills_delete_release_and_candidate(self, httpx_mock):
        """skills.delete_release/delete_candidate should send optional reason in DELETE body."""
        httpx_mock.add_response(
            method="DELETE",
            url="http://localhost:8000/v1/skills/releases/sr-9",
            json={
                "id": "sr-9",
                "deleted_at": "2026-02-08T00:10:00Z",
                "deleted_by": "default",
                "delete_reason": "cleanup",
            },
            status_code=200,
        )
        httpx_mock.add_response(
            method="DELETE",
            url="http://localhost:8000/v1/skills/candidates/sc-9",
            json={
                "id": "sc-9",
                "deleted_at": "2026-02-08T00:11:00Z",
                "deleted_by": "default",
                "delete_reason": None,
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            release_deleted = await client.skills.delete_release("sr-9", reason="cleanup")
            assert release_deleted["id"] == "sr-9"
            assert release_deleted["delete_reason"] == "cleanup"

            candidate_deleted = await client.skills.delete_candidate("sc-9")
            assert candidate_deleted["id"] == "sc-9"
            assert candidate_deleted["delete_reason"] is None

        req_release = httpx_mock.get_requests()[-2]
        body_release = json.loads(req_release.content.decode("utf-8"))
        assert body_release == {"reason": "cleanup"}

        req_candidate = httpx_mock.get_requests()[-1]
        body_candidate = json.loads(req_candidate.content.decode("utf-8"))
        assert body_candidate == {}

    @pytest.mark.asyncio
    async def test_skills_get_release_health(self, httpx_mock):
        """skills.get_release_health should parse health payload."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/releases/sr-1/health",
            json={
                "release_id": "sr-1",
                "skill_key": "browser-login",
                "stage": "canary",
                "window_start_at": "2026-02-08T00:00:00Z",
                "window_end_at": "2026-02-09T00:00:00Z",
                "window_complete": False,
                "samples": 48,
                "success_rate": 0.98,
                "error_rate": 0.02,
                "p95_duration": 2100,
                "baseline_success_rate": 0.99,
                "baseline_error_rate": 0.01,
                "baseline_samples": 120,
                "success_drop": 0.01,
                "error_rate_multiplier": 2.0,
                "healthy": True,
                "should_rollback": False,
                "rollback_reasons": [],
                "thresholds": {"success_drop": 0.03, "error_rate_multiplier": 2.0},
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            health = await client.skills.get_release_health("sr-1")
            assert health.release_id == "sr-1"
            assert health.samples == 48
            assert health.healthy is True

    @pytest.mark.asyncio
    async def test_skills_get_release_health_parses_rollback_signals(self, httpx_mock):
        """skills.get_release_health should parse rollback reason payload."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/releases/sr-risky/health",
            json={
                "release_id": "sr-risky",
                "skill_key": "browser-checkout",
                "stage": "canary",
                "window_start_at": "2026-02-08T00:00:00Z",
                "window_end_at": "2026-02-09T00:00:00Z",
                "window_complete": False,
                "samples": 20,
                "success_rate": 0.70,
                "error_rate": 0.30,
                "p95_duration": 3100,
                "baseline_success_rate": 0.96,
                "baseline_error_rate": 0.04,
                "baseline_samples": 120,
                "success_drop": 0.26,
                "error_rate_multiplier": 7.5,
                "healthy": False,
                "should_rollback": True,
                "rollback_reasons": ["success_rate_drop", "error_rate_regression"],
                "thresholds": {"success_drop": 0.03, "error_rate_multiplier": 2.0},
            },
            status_code=200,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            health = await client.skills.get_release_health("sr-risky")
            assert health.should_rollback is True
            assert health.rollback_reasons == ["success_rate_drop", "error_rate_regression"]

    # -------------------------------------------------------------------------
    # pre/postconditions dual-format compatibility (dict vs list)
    # -------------------------------------------------------------------------

    def _candidate_payload(
        self,
        *,
        preconditions=None,
        postconditions=None,
    ) -> dict:
        return {
            "id": "sc-evo-1",
            "skill_key": "github-get-stars",
            "source_execution_ids": ["exec-1"],
            "status": "draft",
            "created_at": "2026-03-01T00:00:00Z",
            "updated_at": "2026-03-01T00:00:00Z",
            "preconditions": preconditions,
            "postconditions": postconditions,
        }

    @pytest.mark.asyncio
    async def test_get_candidate_parses_list_preconditions(self, httpx_mock):
        """get_candidate must not raise when server returns list[str] pre/postconditions."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/candidates/sc-evo-1",
            json=self._candidate_payload(
                preconditions=["browser available", "JS enabled"],
                postconditions=["integer returned"],
            ),
            status_code=200,
        )
        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            c = await client.skills.get_candidate("sc-evo-1")
        assert c.preconditions == ["browser available", "JS enabled"]
        assert c.postconditions == ["integer returned"]

    @pytest.mark.asyncio
    async def test_get_candidate_parses_dict_preconditions(self, httpx_mock):
        """get_candidate must still work when server returns dict pre/postconditions."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/candidates/sc-evo-1",
            json=self._candidate_payload(
                preconditions={"browser": "available"},
                postconditions={"result": "integer"},
            ),
            status_code=200,
        )
        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            c = await client.skills.get_candidate("sc-evo-1")
        assert c.preconditions == {"browser": "available"}
        assert c.postconditions == {"result": "integer"}

    @pytest.mark.asyncio
    async def test_get_candidate_parses_null_preconditions(self, httpx_mock):
        """get_candidate must handle null pre/postconditions (no conditions set)."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/candidates/sc-evo-1",
            json=self._candidate_payload(),
            status_code=200,
        )
        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            c = await client.skills.get_candidate("sc-evo-1")
        assert c.preconditions is None
        assert c.postconditions is None

    @pytest.mark.asyncio
    async def test_list_candidates_parses_list_preconditions(self, httpx_mock):
        """list_candidates must not raise when evolution candidates have list preconditions."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:8000/v1/skills/candidates?limit=100&offset=0",
            json={
                "items": [
                    self._candidate_payload(
                        preconditions=["browser available"],
                        postconditions=["integer returned"],
                    )
                ],
                "total": 1,
            },
            status_code=200,
        )
        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            result = await client.skills.list_candidates()
        assert result.total == 1
        assert result.items[0].preconditions == ["browser available"]

    @pytest.mark.asyncio
    async def test_create_candidate_accepts_list_preconditions(self, httpx_mock):
        """create_candidate should pass list[str] conditions through unchanged."""
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8000/v1/skills/candidates",
            json=self._candidate_payload(
                preconditions=["browser available", "JS enabled"],
                postconditions=["integer returned"],
            ),
            status_code=201,
        )

        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="test-token",
        ) as client:
            candidate = await client.skills.create_candidate(
                skill_key="github-get-stars",
                source_execution_ids=["exec-1"],
                preconditions=["browser available", "JS enabled"],
                postconditions=["integer returned"],
            )

        request = httpx_mock.get_requests()[0]
        assert request.read().decode() == (
            '{"skill_key":"github-get-stars","source_execution_ids":["exec-1"],'
            '"preconditions":["browser available","JS enabled"],'
            '"postconditions":["integer returned"]}'
        )
        assert candidate.preconditions == ["browser available", "JS enabled"]
        assert candidate.postconditions == ["integer returned"]
