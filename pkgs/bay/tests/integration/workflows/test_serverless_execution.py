"""Serverless-style Quick Execution workflow tests.

Purpose: Simulate quick executions without caring about persistence:
- Minimal API calls (create -> exec -> delete)
- Lazy loading (container not started on create)
- Cold start verification
- Complete cleanup after delete

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 4

Note: workflow 场景测试默认会被 SERIAL_GROUPS["workflows"] 归类为 serial/workflows，
在“两阶段”执行流程的 Phase 2 独占 Bay 跑。
"""

from __future__ import annotations

import asyncio

import httpx

from ..conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    DEFAULT_PROFILE,
    cargo_volume_exists,
    e2e_skipif_marks,
)

pytestmark = e2e_skipif_marks


class TestServerlessExecutionWorkflow:
    """Simple Quick Execution (Stateless Serverless-style)."""

    async def test_minimal_lifecycle_three_api_calls(self):
        """Complete lifecycle with just 3 API calls: create -> exec -> delete."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Step 1: Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox = create_response.json()
            sandbox_id = sandbox["id"]

            # Verify initial state
            assert sandbox["status"] == "idle", f"Expected idle status, got: {sandbox['status']}"

            # Step 2: Execute code (triggers cold start)
            exec_response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print(2 * 21)", "timeout": 30},
                timeout=120.0,
            )
            assert exec_response.status_code == 200
            result = exec_response.json()
            assert result["success"] is True
            assert "42" in result["output"], f"Expected '42' in output, got: {result['output']}"

            # Step 3: Delete sandbox
            delete_response = await client.delete(f"/v1/sandboxes/{sandbox_id}")
            assert delete_response.status_code == 204

            # Verify complete cleanup
            get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert get_response.status_code == 404

    async def test_lazy_loading_container_not_started_on_create(self):
        """Container should NOT be started when sandbox is created."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox = create_response.json()
            sandbox_id = sandbox["id"]

            try:
                assert sandbox["status"] == "idle"

                get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_response.status_code == 200
                assert get_response.json()["status"] == "idle"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_cold_start_on_first_exec(self):
        """First execution should trigger cold start."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                get_before = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_before.json()["status"] == "idle"

                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print('cold started!')", "timeout": 30},
                    timeout=120.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()
                assert result["success"] is True
                assert "cold started!" in result["output"]

                get_after = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_after.json()["status"] in ("ready", "starting")

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_delete_cleans_up_all_resources(self):
        """Delete should clean up cargo volume."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox = create_response.json()
            sandbox_id = sandbox["id"]
            cargo_id = sandbox["cargo_id"]

            # Verify volume/PVC was created (works for both Docker and K8s)
            assert cargo_volume_exists(cargo_id), (
                f"Volume for cargo {cargo_id} should exist after create"
            )

            # Execute to start container
            await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print('hello')", "timeout": 30},
                timeout=120.0,
            )
            await asyncio.sleep(0.5)

            delete_response = await client.delete(f"/v1/sandboxes/{sandbox_id}")
            assert delete_response.status_code == 204

            await asyncio.sleep(1.0)
            assert not cargo_volume_exists(cargo_id), (
                f"Volume for cargo {cargo_id} should be deleted after delete"
            )
            get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
            assert get_response.status_code == 404

    async def test_sequential_sandboxes_independent(self):
        """Multiple sandboxes should be independent."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            sandbox_ids: list[str] = []

            try:
                for _ in range(3):
                    r = await client.post(
                        "/v1/sandboxes",
                        json={"profile": DEFAULT_PROFILE},
                    )
                    assert r.status_code == 201
                    sandbox_ids.append(r.json()["id"])

                for i, sid in enumerate(sandbox_ids):
                    exec_response = await client.post(
                        f"/v1/sandboxes/{sid}/python/exec",
                        json={"code": f"x = {i}; print(f'sandbox {i}: x={{x}}')", "timeout": 30},
                        timeout=120.0,
                    )
                    assert exec_response.status_code == 200
                    result = exec_response.json()
                    assert result["success"] is True
                    assert f"sandbox {i}: x={i}" in result["output"]

                for sid in reversed(sandbox_ids):
                    d = await client.delete(f"/v1/sandboxes/{sid}")
                    assert d.status_code == 204

            finally:
                for sid in sandbox_ids:
                    await client.delete(f"/v1/sandboxes/{sid}")

    async def test_compute_intensive_task(self):
        """Execute a CPU-intensive task to verify execution works."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                compute_code = """
import math

def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(math.sqrt(n)) + 1):
        if n % i == 0:
            return False
    return True

primes = []
n = 2
while len(primes) < 100:
    if is_prime(n):
        primes.append(n)
    n += 1

print(f"Sum of first 100 primes: {sum(primes)}")
print(f"100th prime: {primes[-1]}")
"""
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": compute_code, "timeout": 60},
                    timeout=120.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()
                assert result["success"] is True
                assert "24133" in result["output"]
                assert "541" in result["output"]

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_oneshot_json_processing(self):
        """Process JSON data in a single execution - typical serverless use case."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                json_processing_code = """
import json

data = {
    "users": [
        {"name": "Alice", "age": 30, "active": True},
        {"name": "Bob", "age": 25, "active": False},
        {"name": "Charlie", "age": 35, "active": True}
    ]
}

active_users = [u for u in data["users"] if u["active"]]
avg_age = sum(u["age"] for u in active_users) / len(active_users)

result = {
    "active_count": len(active_users),
    "average_age": avg_age,
    "names": [u["name"] for u in active_users]
}

print(json.dumps(result))
"""
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": json_processing_code, "timeout": 30},
                    timeout=120.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()
                assert result["success"] is True

                import json

                output_data = json.loads(result["output"].strip())
                assert output_data["active_count"] == 2
                assert output_data["average_age"] == 32.5
                assert "Alice" in output_data["names"]
                assert "Charlie" in output_data["names"]

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
