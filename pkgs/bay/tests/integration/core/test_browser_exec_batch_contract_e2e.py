"""E2E contract tests for browser exec_batch semantics.

Focus:
- success/completed_steps/total_steps contract on partial failure
- history success flag mirrors API response
- trace payload summary fields mirror API response
"""

from __future__ import annotations

import subprocess

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


async def test_browser_exec_batch_contract_on_partial_failure_and_trace_alignment():
    _require_browser_runtime()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        browser_profile = await _resolve_browser_profile(client)
        async with create_sandbox(client, profile=browser_profile) as sandbox:
            sandbox_id = sandbox["id"]

            batch_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/browser/exec_batch",
                json={
                    "commands": [
                        "open about:blank",
                        "nonexistent-subcommand",
                        "get title",
                    ],
                    "timeout": 120,
                    "stop_on_error": True,
                    "description": "browser batch contract partial failure",
                    "tags": "skill:e2e-browser-batch-contract",
                    "learn": True,
                    "include_trace": True,
                },
                timeout=180.0,
            )
            assert batch_resp.status_code == 200, batch_resp.text
            batch_data = batch_resp.json()

            assert batch_data["total_steps"] == 3
            assert batch_data["completed_steps"] == 2
            assert batch_data["success"] is False
            assert len(batch_data["results"]) == 2
            assert batch_data["results"][-1]["cmd"] == "nonexistent-subcommand"
            assert batch_data["results"][-1]["exit_code"] != 0

            history_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/{batch_data['execution_id']}",
                timeout=30.0,
            )
            assert history_resp.status_code == 200, history_resp.text
            history = history_resp.json()
            assert history["exec_type"] == "browser_batch"
            assert history["success"] is False
            assert history["payload_ref"] == batch_data["trace_ref"]

            trace_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/browser/traces/{batch_data['trace_ref']}",
                timeout=30.0,
            )
            assert trace_resp.status_code == 200, trace_resp.text
            trace = trace_resp.json()["trace"]
            assert trace["kind"] == "browser_batch_trace"
            assert trace["total_steps"] == batch_data["total_steps"]
            assert trace["completed_steps"] == batch_data["completed_steps"]
            assert trace["success"] is batch_data["success"]
            assert len(trace["steps"]) == batch_data["completed_steps"]
