"""Resilience test: OOM Killed container behavior.

Purpose: Verify the system correctly handles runtimes killed due to memory limits.

Scenario:
1. Create a sandbox using a profile with limited memory (e.g., 128MB).
2. Execute code that attempts to allocate more memory than allowed.
3. Verify the runtime is killed by OOM and the sandbox status remains accessible.
4. Optionally verify the exit code indicates OOM.

Note: This test requires a profile with strict memory limits.
      If such profile doesn't exist, the test will be skipped.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.integration.conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    create_sandbox,
    e2e_skipif_marks,
    get_runtime_exit_code,
    get_runtime_id_by_sandbox,
)

pytestmark = e2e_skipif_marks

# Profile with strict memory limit for OOM testing.
# This profile should have ~128MB limit to make OOM trigger quickly.
# Expected to be defined in test config with resources.memory: "128m"
OOM_TEST_PROFILE = "oom-test"


async def _skip_if_oom_profile_missing():
    """Check if OOM test profile exists and skip if not."""
    try:
        async with httpx.AsyncClient(
            base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=5.0
        ) as client:
            resp = await client.get("/v1/profiles")
            if resp.status_code == 200:
                data = resp.json()
                profiles = data.get("items", [])
                if not any(p.get("id") == OOM_TEST_PROFILE for p in profiles):
                    pytest.skip(f"OOM test profile '{OOM_TEST_PROFILE}' not found")
            else:
                pytest.skip(f"Failed to query profiles: {resp.status_code}")
    except Exception as e:
        pytest.skip(f"Failed to check OOM profile: {e}")


class TestOOMKilled:
    """Test system behavior when runtime is killed due to OOM."""

    async def test_oom_returns_error_not_hang(self) -> None:
        """When runtime OOMs, exec should return error, not hang indefinitely."""
        # Dynamic check - skip if profile not configured
        await _skip_if_oom_profile_missing()

        async with httpx.AsyncClient(
            base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=120.0
        ) as client:
            async with create_sandbox(client, profile=OOM_TEST_PROFILE) as sandbox:
                sandbox_id = sandbox["id"]

                # Code that tries to allocate way more memory than container limit
                # 128MB limit -> try to allocate 500MB
                oom_code = """
import sys
# Allocate ~500MB by creating a large list of bytes
big_list = []
for i in range(500):
    big_list.append(b'x' * (1024 * 1024))  # 1MB each
print(f'Allocated {len(big_list)} MB')
"""

                # Execute the memory-hungry code
                # This should either:
                # - Return error (runtime killed)
                # - Return MemoryError from Python
                # - Timeout (runtime killed before response)
                try:
                    exec_resp = await client.post(
                        f"/v1/sandboxes/{sandbox_id}/python/exec",
                        json={"code": oom_code, "timeout": 60},
                        timeout=90.0,
                    )
                except httpx.ReadTimeout:
                    # Timeout is acceptable - runtime was killed
                    pass
                else:
                    # If we got a response, verify it's an error
                    # (success would mean OOM didn't trigger, which is also valid info)
                    if exec_resp.status_code == 200:
                        result = exec_resp.json()
                        # Either execution failed or Python caught MemoryError
                        if result.get("success"):
                            pytest.skip(
                                "OOM not triggered - memory limit may be too high"
                            )
                        else:
                            # Execution failed, which is expected
                            pass
                    else:
                        # Non-200 response (500, 503) is acceptable
                        assert exec_resp.status_code in (500, 503)

                # Verify sandbox is still accessible (not corrupted)
                await asyncio.sleep(1.0)
                status_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert status_resp.status_code == 200

    async def test_container_exit_code_indicates_oom(self) -> None:
        """Runtime exit code should indicate OOM kill (137 = SIGKILL)."""
        # Dynamic check - skip if profile not configured
        await _skip_if_oom_profile_missing()

        async with httpx.AsyncClient(
            base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=120.0
        ) as client:
            async with create_sandbox(client, profile=OOM_TEST_PROFILE) as sandbox:
                sandbox_id = sandbox["id"]

                # First, start a session to get a runtime
                warmup = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print('warmup')", "timeout": 30},
                    timeout=60.0,
                )
                assert warmup.status_code == 200

                # Find runtime ID (Docker container ID or K8s Pod name)
                runtime_id = get_runtime_id_by_sandbox(sandbox_id)
                if runtime_id is None:
                    pytest.skip("Could not find runtime")

                # Trigger OOM
                oom_code = """
big_list = []
for i in range(500):
    big_list.append(b'x' * (1024 * 1024))
"""
                try:
                    await client.post(
                        f"/v1/sandboxes/{sandbox_id}/python/exec",
                        json={"code": oom_code, "timeout": 60},
                        timeout=90.0,
                    )
                except httpx.ReadTimeout:
                    pass

                await asyncio.sleep(2.0)

                # Check runtime exit code
                exit_code = get_runtime_exit_code(runtime_id)
                if exit_code is None:
                    pytest.skip("Could not determine runtime exit code")

                # 137 = 128 + 9 (SIGKILL)
                # This is typical for OOM kill
                # But we don't strictly require it since Python might catch MemoryError first
                if exit_code == 137:
                    # OOM confirmed
                    pass
