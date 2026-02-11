"""Skill lifecycle API integration tests.

Purpose: Verify candidate/evaluation/release endpoints over real Bay API.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def _create_python_execution(client: httpx.AsyncClient, sandbox_id: str, code: str) -> str:
    resp = await client.post(
        f"/v1/sandboxes/{sandbox_id}/python/exec",
        json={"code": code, "tags": "skills,evidence"},
        timeout=120.0,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_id"].startswith("exec-")
    return data["execution_id"]


async def test_candidate_evaluate_promote_and_rollback_flow():
    """Full happy path for skill lifecycle including rollback."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            exec_a = await _create_python_execution(client, sandbox_id, "print('candidate-a')")
            create_a = await client.post(
                "/v1/skills/candidates",
                json={
                    "skill_key": "csv-loader",
                    "source_execution_ids": [exec_a],
                    "scenario_key": "etl.csv",
                    "payload_ref": "s3://skills/csv-loader/a",
                },
            )
            assert create_a.status_code == 201
            candidate_a = create_a.json()
            assert candidate_a["status"] == "draft"
            assert candidate_a["source_execution_ids"] == [exec_a]

            evaluate_a = await client.post(
                f"/v1/skills/candidates/{candidate_a['id']}/evaluate",
                json={
                    "passed": True,
                    "score": 0.93,
                    "benchmark_id": "bench-csv-v1",
                    "report": "pass",
                },
            )
            assert evaluate_a.status_code == 200
            assert evaluate_a.json()["passed"] is True

            promote_a = await client.post(
                f"/v1/skills/candidates/{candidate_a['id']}/promote",
                json={"stage": "stable"},
            )
            assert promote_a.status_code == 200
            release_a = promote_a.json()
            assert release_a["skill_key"] == "csv-loader"
            assert release_a["version"] == 1
            assert release_a["stage"] == "stable"
            assert release_a["is_active"] is True

            exec_b = await _create_python_execution(client, sandbox_id, "print('candidate-b')")
            create_b = await client.post(
                "/v1/skills/candidates",
                json={
                    "skill_key": "csv-loader",
                    "source_execution_ids": [exec_b],
                    "scenario_key": "etl.csv",
                },
            )
            assert create_b.status_code == 201
            candidate_b = create_b.json()

            evaluate_b = await client.post(
                f"/v1/skills/candidates/{candidate_b['id']}/evaluate",
                json={"passed": True, "score": 0.98, "benchmark_id": "bench-csv-v2"},
            )
            assert evaluate_b.status_code == 200
            promote_b = await client.post(
                f"/v1/skills/candidates/{candidate_b['id']}/promote",
                json={"stage": "canary"},
            )
            assert promote_b.status_code == 200
            release_b = promote_b.json()
            assert release_b["version"] == 2
            assert release_b["is_active"] is True

            list_active = await client.get(
                "/v1/skills/releases",
                params={"skill_key": "csv-loader", "active_only": True},
            )
            assert list_active.status_code == 200
            active_data = list_active.json()
            assert active_data["total"] == 1
            assert active_data["items"][0]["id"] == release_b["id"]

            rollback = await client.post(f"/v1/skills/releases/{release_b['id']}/rollback")
            assert rollback.status_code == 200
            rollback_release = rollback.json()
            assert rollback_release["rollback_of"] == release_b["id"]
            assert rollback_release["version"] == 3
            assert rollback_release["is_active"] is True

            get_candidate_b = await client.get(f"/v1/skills/candidates/{candidate_b['id']}")
            assert get_candidate_b.status_code == 200
            assert get_candidate_b.json()["status"] == "rolled_back"


async def test_skill_lifecycle_list_filters_and_pagination():
    """List endpoints should support filters and pagination."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            exec_a = await _create_python_execution(client, sandbox_id, "print('list-a')")
            exec_b = await _create_python_execution(client, sandbox_id, "print('list-b')")

            create_a = await client.post(
                "/v1/skills/candidates",
                json={"skill_key": "loader-a", "source_execution_ids": [exec_a]},
            )
            create_b = await client.post(
                "/v1/skills/candidates",
                json={"skill_key": "loader-b", "source_execution_ids": [exec_b]},
            )
            assert create_a.status_code == 201
            assert create_b.status_code == 201
            candidate_a = create_a.json()
            candidate_b = create_b.json()

            reject_b = await client.post(
                f"/v1/skills/candidates/{candidate_b['id']}/evaluate",
                json={"passed": False, "score": 0.12},
            )
            assert reject_b.status_code == 200

            by_key = await client.get(
                "/v1/skills/candidates",
                params={"skill_key": "loader-a", "limit": 10, "offset": 0},
            )
            assert by_key.status_code == 200
            by_key_data = by_key.json()
            assert by_key_data["total"] == 1
            assert by_key_data["items"][0]["id"] == candidate_a["id"]

            rejected = await client.get(
                "/v1/skills/candidates",
                params={"status": "rejected", "limit": 10, "offset": 0},
            )
            assert rejected.status_code == 200
            rejected_data = rejected.json()
            assert rejected_data["total"] == 1
            assert rejected_data["items"][0]["id"] == candidate_b["id"]

            paged = await client.get("/v1/skills/candidates", params={"limit": 1, "offset": 0})
            assert paged.status_code == 200
            assert len(paged.json()["items"]) == 1


async def test_promote_requires_passing_evaluation():
    """Promoting a non-passing candidate should return conflict."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]
            exec_id = await _create_python_execution(client, sandbox_id, "print('no-eval')")

            create_resp = await client.post(
                "/v1/skills/candidates",
                json={"skill_key": "blocked-skill", "source_execution_ids": [exec_id]},
            )
            assert create_resp.status_code == 201
            candidate_id = create_resp.json()["id"]

            promote_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/promote",
                json={"stage": "canary"},
            )
            assert promote_resp.status_code == 409


async def test_skill_api_validation_errors():
    """Skill APIs should reject invalid status/stage and missing source execution IDs."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        invalid_status = await client.get("/v1/skills/candidates", params={"status": "unknown"})
        assert invalid_status.status_code == 400

        invalid_stage_list = await client.get("/v1/skills/releases", params={"stage": "unknown"})
        assert invalid_stage_list.status_code == 400

        invalid_create = await client.post(
            "/v1/skills/candidates",
            json={"skill_key": "bad", "source_execution_ids": ["exec-missing"]},
        )
        assert invalid_create.status_code == 400

        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]
            exec_id = await _create_python_execution(client, sandbox_id, "print('stage-check')")
            candidate = await client.post(
                "/v1/skills/candidates",
                json={"skill_key": "stage-check", "source_execution_ids": [exec_id]},
            )
            assert candidate.status_code == 201
            candidate_id = candidate.json()["id"]

            bad_stage_promote = await client.post(
                f"/v1/skills/candidates/{candidate_id}/promote",
                json={"stage": "invalid-stage"},
            )
            assert bad_stage_promote.status_code == 400


async def test_release_health_endpoint_returns_policy_metrics():
    """Release health endpoint should expose canary metrics and rollback policy fields."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]
            exec_id = await _create_python_execution(client, sandbox_id, "print('health-release')")

            candidate_resp = await client.post(
                "/v1/skills/candidates",
                json={
                    "skill_key": "health-check-skill",
                    "source_execution_ids": [exec_id],
                },
            )
            assert candidate_resp.status_code == 201
            candidate_id = candidate_resp.json()["id"]

            evaluate_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/evaluate",
                json={"passed": True, "score": 0.95},
            )
            assert evaluate_resp.status_code == 200

            promote_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/promote",
                json={"stage": "canary"},
            )
            assert promote_resp.status_code == 200
            release_id = promote_resp.json()["id"]

            health_resp = await client.get(f"/v1/skills/releases/{release_id}/health")
            assert health_resp.status_code == 200
            data = health_resp.json()
            assert data["release_id"] == release_id
            assert "success_rate" in data
            assert "error_rate" in data
            assert "p95_duration" in data
            assert "should_rollback" in data


async def test_skill_payload_create_and_get_round_trip():
    """Skills payload API should create and read payloads via blob references."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        create_resp = await client.post(
            "/v1/skills/payloads",
            json={
                "kind": "candidate_payload",
                "payload": {"commands": ["open about:blank", "snapshot -i"]},
            },
            timeout=30.0,
        )
        assert create_resp.status_code == 201, create_resp.text
        create_data = create_resp.json()
        assert create_data["payload_ref"].startswith("blob:")
        assert create_data["kind"] == "candidate_payload"

        payload_ref = create_data["payload_ref"]
        get_resp = await client.get(f"/v1/skills/payloads/{payload_ref}", timeout=30.0)
        assert get_resp.status_code == 200, get_resp.text
        get_data = get_resp.json()
        assert get_data["payload_ref"] == payload_ref
        assert get_data["kind"] == "candidate_payload"
        assert get_data["payload"] == {"commands": ["open about:blank", "snapshot -i"]}


async def test_skill_payload_get_rejects_non_blob_reference():
    """Skills payload read API should reject non-blob references."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        bad_ref = quote("s3://candidate/payload-1", safe="")
        resp = await client.get(f"/v1/skills/payloads/{bad_ref}", timeout=30.0)
        assert resp.status_code == 400
        assert "Unsupported payload_ref" in resp.text


async def test_skill_payload_get_returns_not_found_for_unknown_blob():
    """Skills payload read API should return not found for missing blob refs."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/v1/skills/payloads/blob:missing-payload", timeout=30.0)
        assert resp.status_code == 404
