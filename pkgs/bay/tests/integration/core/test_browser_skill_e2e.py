"""Browser skill lifecycle E2E tests for new exec/trace/replay APIs.

Parallel-safe: Yes - each test owns sandbox lifecycle.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import httpx
import pytest

from ..conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    E2E_DRIVER_TYPE,
    create_sandbox,
    e2e_skipif_marks,
)

pytestmark = e2e_skipif_marks


def _docker_image_exists(image: str) -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _require_browser_runtime() -> None:
    if E2E_DRIVER_TYPE == "docker" and not _docker_image_exists("gull:latest"):
        pytest.skip("gull:latest image not available")


async def _resolve_browser_profile(client: httpx.AsyncClient) -> str:
    resp = await client.get("/v1/profiles", timeout=30.0)
    assert resp.status_code == 200, resp.text
    items = resp.json().get("items", [])
    for item in items:
        capabilities = item.get("capabilities") or []
        if "browser" in capabilities:
            return str(item["id"])
    pytest.skip("No browser-enabled profile available")


async def _wait_for_learning_completion(
    client: httpx.AsyncClient,
    *,
    sandbox_id: str,
    execution_id: str,
    timeout_seconds: float = 30.0,
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_status = "unknown"
    while asyncio.get_running_loop().time() < deadline:
        history_resp = await client.get(
            f"/v1/sandboxes/{sandbox_id}/history/{execution_id}",
            timeout=30.0,
        )
        assert history_resp.status_code == 200, history_resp.text
        learn_status = str(history_resp.json().get("learn_status") or "").lower()
        if learn_status in {"processed", "skipped", "error"}:
            return learn_status
        if learn_status:
            last_status = learn_status
        await asyncio.sleep(1.0)
    pytest.skip(
        "Browser learning scheduler did not process execution within timeout. "
        "Configure fast test interval via browser_learning.interval_seconds."
    )
    return last_status


async def test_browser_exec_trace_and_history_round_trip():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]
            exec_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec",
                json={
                    "cmd": "open about:blank",
                    "description": "browser exec trace round trip",
                    "tags": "skill:e2e-browser-exec,trace",
                    "learn": True,
                    "include_trace": True,
                },
                timeout=120.0,
            )
            assert exec_resp.status_code == 200, exec_resp.text
            exec_data = exec_resp.json()
            assert exec_data["execution_id"].startswith("exec-")
            assert exec_data["execution_time_ms"] >= 0
            assert exec_data["trace_ref"].startswith("blob:")

            history_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/{exec_data['execution_id']}",
                timeout=30.0,
            )
            assert history_resp.status_code == 200, history_resp.text
            history = history_resp.json()
            assert history["exec_type"] == "browser"
            assert history["learn_enabled"] is True
            assert history["payload_ref"] == exec_data["trace_ref"]
            assert history["description"] == "browser exec trace round trip"
            assert "skill:e2e-browser-exec" in (history["tags"] or "")

            trace_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/browser/traces/{exec_data['trace_ref']}",
                timeout=30.0,
            )
            assert trace_resp.status_code == 200, trace_resp.text
            trace = trace_resp.json()["trace"]
            assert trace["kind"] == "browser_exec_trace"
            assert trace["steps"][0]["cmd"] == "open about:blank"

            payload_resp = await client.get(
                f"/v1/skills/payloads/{exec_data['trace_ref']}",
                timeout=30.0,
            )
            assert payload_resp.status_code == 200, payload_resp.text
            payload_data = payload_resp.json()
            assert payload_data["payload_ref"] == exec_data["trace_ref"]
            assert payload_data["payload"] == trace


async def test_browser_exec_learn_only_stores_payload_ref_without_returning_trace_ref():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]
            exec_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec",
                json={
                    "cmd": "open about:blank",
                    "learn": True,
                    "include_trace": False,
                    "tags": "skill:e2e-browser-learn-only",
                },
                timeout=120.0,
            )
            assert exec_resp.status_code == 200, exec_resp.text
            exec_data = exec_resp.json()
            assert exec_data["execution_id"].startswith("exec-")
            assert exec_data["trace_ref"] is None

            history_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/{exec_data['execution_id']}",
                timeout=30.0,
            )
            assert history_resp.status_code == 200
            history = history_resp.json()
            assert history["payload_ref"] is not None
            assert history["payload_ref"].startswith("blob:")

            trace_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/browser/traces/{history['payload_ref']}",
                timeout=30.0,
            )
            assert trace_resp.status_code == 200
            assert trace_resp.json()["trace_ref"] == history["payload_ref"]


async def test_browser_exec_batch_trace_and_history_round_trip():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]
            batch_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec_batch",
                json={
                    "commands": ["open about:blank", "snapshot -i"],
                    "timeout": 120,
                    "stop_on_error": True,
                    "description": "browser batch trace round trip",
                    "tags": "skill:e2e-browser-batch,trace",
                    "learn": True,
                    "include_trace": True,
                },
                timeout=180.0,
            )
            assert batch_resp.status_code == 200, batch_resp.text
            batch_data = batch_resp.json()
            assert batch_data["execution_id"].startswith("exec-")
            assert batch_data["execution_time_ms"] >= 0
            assert batch_data["trace_ref"].startswith("blob:")
            assert batch_data["total_steps"] >= 1
            assert batch_data["completed_steps"] >= 1

            history_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/{batch_data['execution_id']}",
                timeout=30.0,
            )
            assert history_resp.status_code == 200
            history = history_resp.json()
            assert history["exec_type"] == "browser_batch"
            assert history["payload_ref"] == batch_data["trace_ref"]
            assert history["learn_enabled"] is True

            trace_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/browser/traces/{batch_data['trace_ref']}",
                timeout=30.0,
            )
            assert trace_resp.status_code == 200
            trace = trace_resp.json()["trace"]
            assert trace["kind"] == "browser_batch_trace"
            assert len(trace["steps"]) >= 1


async def test_browser_exec_batch_failure_keeps_response_history_trace_consistent():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]
            batch_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec_batch",
                json={
                    "commands": ["open about:blank", "nonexistent-subcommand", "get title"],
                    "timeout": 120,
                    "stop_on_error": True,
                    "description": "browser batch failure parity check",
                    "tags": "skill:e2e-browser-batch,trace,failure",
                    "learn": True,
                    "include_trace": True,
                },
                timeout=180.0,
            )
            assert batch_resp.status_code == 200, batch_resp.text
            batch_data = batch_resp.json()
            assert batch_data["success"] is False
            assert batch_data["total_steps"] == 3
            assert batch_data["completed_steps"] < batch_data["total_steps"]

            history_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/{batch_data['execution_id']}",
                timeout=30.0,
            )
            assert history_resp.status_code == 200
            history = history_resp.json()
            assert history["exec_type"] == "browser_batch"
            assert history["success"] is False
            assert history["payload_ref"] == batch_data["trace_ref"]

            trace_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/browser/traces/{batch_data['trace_ref']}",
                timeout=30.0,
            )
            assert trace_resp.status_code == 200
            trace = trace_resp.json()["trace"]
            assert trace["kind"] == "browser_batch_trace"
            assert trace["success"] is batch_data["success"]
            assert trace["total_steps"] == batch_data["total_steps"]
            assert trace["completed_steps"] == batch_data["completed_steps"]


async def test_browser_trace_endpoint_returns_not_found_for_unknown_trace():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]
            resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/browser/traces/blob:missing-trace",
                timeout=30.0,
            )
            assert resp.status_code == 404


async def test_browser_run_skill_without_active_release_returns_not_found():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]
            resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/skills/missing-skill/run",
                json={"timeout": 60, "stop_on_error": True, "include_trace": False},
                timeout=60.0,
            )
            assert resp.status_code == 404
            assert "No active release found" in resp.text


async def test_browser_run_skill_with_invalid_payload_ref_returns_bad_request():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]

            source_exec_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec",
                json={"cmd": "open about:blank", "timeout": 60},
                timeout=120.0,
            )
            assert source_exec_resp.status_code == 200, source_exec_resp.text
            source_execution_id = source_exec_resp.json()["execution_id"]

            candidate_resp = await client.post(
                "/v1/skills/candidates",
                json={
                    "skill_key": "browser-invalid-payload",
                    "source_execution_ids": [source_execution_id],
                    "payload_ref": "s3://invalid-payload",
                },
                timeout=60.0,
            )
            assert candidate_resp.status_code == 201, candidate_resp.text
            candidate_id = candidate_resp.json()["id"]

            evaluate_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/evaluate",
                json={"passed": True, "score": 0.9},
                timeout=60.0,
            )
            assert evaluate_resp.status_code == 200, evaluate_resp.text

            promote_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/promote",
                json={"stage": "canary"},
                timeout=60.0,
            )
            assert promote_resp.status_code == 200, promote_resp.text

            run_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/skills/browser-invalid-payload/run",
                json={"timeout": 60, "stop_on_error": True, "include_trace": False},
                timeout=60.0,
            )
            assert run_resp.status_code == 400
            assert "Unsupported payload_ref" in run_resp.text


async def test_browser_run_skill_works_with_payload_created_by_generic_payload_api():
    """Generic skills payload API should integrate with candidate -> release -> run flow."""
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]

            payload_resp = await client.post(
                "/v1/skills/payloads",
                json={
                    "kind": "candidate_payload",
                    "payload": {"commands": ["open about:blank"]},
                },
                timeout=60.0,
            )
            assert payload_resp.status_code == 201, payload_resp.text
            payload_ref = payload_resp.json()["payload_ref"]
            assert payload_ref.startswith("blob:")

            source_exec_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec",
                json={"cmd": "open about:blank", "timeout": 60},
                timeout=120.0,
            )
            assert source_exec_resp.status_code == 200, source_exec_resp.text
            source_execution_id = source_exec_resp.json()["execution_id"]

            candidate_resp = await client.post(
                "/v1/skills/candidates",
                json={
                    "skill_key": "browser-generic-payload",
                    "source_execution_ids": [source_execution_id],
                    "payload_ref": payload_ref,
                },
                timeout=60.0,
            )
            assert candidate_resp.status_code == 201, candidate_resp.text
            candidate_id = candidate_resp.json()["id"]

            evaluate_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/evaluate",
                json={"passed": True, "score": 0.91},
                timeout=60.0,
            )
            assert evaluate_resp.status_code == 200, evaluate_resp.text

            promote_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/promote",
                json={"stage": "canary"},
                timeout=60.0,
            )
            assert promote_resp.status_code == 200, promote_resp.text

            run_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/skills/browser-generic-payload/run",
                json={"timeout": 60, "stop_on_error": True, "include_trace": False},
                timeout=120.0,
            )
            assert run_resp.status_code == 200, run_resp.text
            run_data = run_resp.json()
            assert run_data["execution_id"].startswith("exec-")
            assert run_data["total_steps"] == 1
            assert run_data["completed_steps"] == 1
            assert run_data["results"][0]["cmd"] == "open about:blank"


async def test_browser_run_skill_works_with_payload_containing_variables_field():
    """Candidate payload with variables metadata should remain runnable end-to-end."""
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]

            payload_resp = await client.post(
                "/v1/skills/payloads",
                json={
                    "kind": "candidate_payload",
                    "payload": {
                        "commands": ["open about:blank"],
                        "variables": {
                            "departure_city": {
                                "type": "string",
                                "default_value": "Edinburgh",
                                "action_index": 0,
                                "arg_position": 1,
                            }
                        },
                    },
                },
                timeout=60.0,
            )
            assert payload_resp.status_code == 201, payload_resp.text
            payload_ref = payload_resp.json()["payload_ref"]

            payload_get_resp = await client.get(
                f"/v1/skills/payloads/{payload_ref}",
                timeout=60.0,
            )
            assert payload_get_resp.status_code == 200, payload_get_resp.text
            payload_data = payload_get_resp.json()["payload"]
            assert payload_data["variables"]["departure_city"]["default_value"] == "Edinburgh"

            source_exec_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec",
                json={"cmd": "open about:blank", "timeout": 60},
                timeout=120.0,
            )
            assert source_exec_resp.status_code == 200, source_exec_resp.text
            source_execution_id = source_exec_resp.json()["execution_id"]

            candidate_resp = await client.post(
                "/v1/skills/candidates",
                json={
                    "skill_key": "browser-variable-payload",
                    "source_execution_ids": [source_execution_id],
                    "payload_ref": payload_ref,
                },
                timeout=60.0,
            )
            assert candidate_resp.status_code == 201, candidate_resp.text
            candidate_id = candidate_resp.json()["id"]

            evaluate_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/evaluate",
                json={"passed": True, "score": 0.92},
                timeout=60.0,
            )
            assert evaluate_resp.status_code == 200, evaluate_resp.text

            promote_resp = await client.post(
                f"/v1/skills/candidates/{candidate_id}/promote",
                json={"stage": "canary"},
                timeout=60.0,
            )
            assert promote_resp.status_code == 200, promote_resp.text

            run_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/skills/browser-variable-payload/run",
                json={"timeout": 60, "stop_on_error": True, "include_trace": False},
                timeout=120.0,
            )
            assert run_resp.status_code == 200, run_resp.text
            run_data = run_resp.json()
            assert run_data["execution_id"].startswith("exec-")
            assert run_data["total_steps"] == 1
            assert run_data["completed_steps"] == 1


async def test_browser_learning_deduplicates_candidates_by_payload_hash():
    """Automatic learning should skip duplicate candidates for same skill+payload."""
    _require_browser_runtime()
    skill_key = f"e2e-browser-learning-dedup-{uuid.uuid4().hex[:8]}"
    commands = ["open about:blank", "open about:blank"]

    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]

            exec_ids: list[str] = []
            for _ in range(2):
                resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/browser/exec_batch",
                    json={
                        "commands": commands,
                        "timeout": 120,
                        "stop_on_error": True,
                        "learn": True,
                        "include_trace": True,
                        "tags": f"skill:{skill_key},learn,e2e",
                    },
                    timeout=180.0,
                )
                assert resp.status_code == 200, resp.text
                exec_ids.append(resp.json()["execution_id"])

            statuses = [
                await _wait_for_learning_completion(
                    client,
                    sandbox_id=sandbox_id,
                    execution_id=execution_id,
                )
                for execution_id in exec_ids
            ]
            assert statuses == ["processed", "processed"]

            candidates_resp = await client.get(
                "/v1/skills/candidates",
                params={"skill_key": skill_key, "limit": 50, "offset": 0},
                timeout=30.0,
            )
            assert candidates_resp.status_code == 200, candidates_resp.text
            candidates_data = candidates_resp.json()
            assert candidates_data["total"] == 1
            candidate = candidates_data["items"][0]
            assert candidate["skill_key"] == skill_key

            payload_resp = await client.get(
                f"/v1/skills/payloads/{candidate['payload_ref']}",
                timeout=30.0,
            )
            assert payload_resp.status_code == 200, payload_resp.text
            payload = payload_resp.json()["payload"]
            assert payload["commands"] == commands
            assert len(str(payload["payload_hash"])) == 64
