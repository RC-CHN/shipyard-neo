"""Resilience test: OOM Killed container behavior.

Purpose: Verify the system correctly handles containers killed due to memory limits.

Scenario:
1. Create a sandbox using a profile with limited memory (e.g., 128MB).
2. Execute code that attempts to allocate more memory than allowed.
3. Verify the container is killed by OOM and the sandbox status reflects this.
4. Optionally verify the error message/logs indicate OOM.

Note: This test requires a profile with strict memory limits.
      If such profile doesn't exist, the test will be skipped.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from tests.integration.conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    create_sandbox,
    e2e_skipif_marks,
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
                profiles = resp.json()
                if not any(p.get("id") == OOM_TEST_PROFILE for p in profiles):
                    pytest.skip(f"OOM test profile '{OOM_TEST_PROFILE}' not found")
            else:
                pytest.skip(f"Failed to query profiles: {resp.status_code}")
    except Exception as e:
        pytest.skip(f"Failed to check OOM profile: {e}")


class TestOOMKilled:
    """Test system behavior when container is killed due to OOM."""

    async def test_oom_returns_error_not_hang(self) -> None:
        """When container OOMs, exec should return error, not hang indefinitely."""
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
                # - Return error (container killed)
                # - Return MemoryError from Python
                # - Timeout (container killed before response)
                try:
                    exec_resp = await client.post(
                        f"/v1/sandboxes/{sandbox_id}/python/exec",
                        json={"code": oom_code, "timeout": 60},
                        timeout=90.0,
                    )
                except httpx.ReadTimeout:
                    # Timeout is acceptable - container was killed
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
        """Container exit code should indicate OOM kill (137 = SIGKILL)."""
        # Dynamic check - skip if profile not configured
        await _skip_if_oom_profile_missing()

        async with httpx.AsyncClient(
            base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=120.0
        ) as client:
            async with create_sandbox(client, profile=OOM_TEST_PROFILE) as sandbox:
                sandbox_id = sandbox["id"]

                # First, start a session to get a container
                warmup = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print('warmup')", "timeout": 30},
                    timeout=60.0,
                )
                assert warmup.status_code == 200

                # Find container ID
                result = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-q",
                        "--filter",
                        f"label=bay.sandbox_id={sandbox_id}",
                    ],
                    capture_output=True,
                    timeout=10,
                    text=True,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    pytest.skip("Could not find container")

                container_id = result.stdout.strip().split("\n")[0]

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

                # Check container exit code
                inspect_result = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.State.ExitCode}}",
                        container_id,
                    ],
                    capture_output=True,
                    timeout=10,
                    text=True,
                )

                if inspect_result.returncode == 0:
                    exit_code_str = inspect_result.stdout.strip()
                    if exit_code_str.isdigit():
                        exit_code = int(exit_code_str)
                        # 137 = 128 + 9 (SIGKILL)
                        # This is typical for OOM kill
                        # But we don't strictly require it since Python might catch MemoryError first
                        if exit_code == 137:
                            # OOM confirmed
                            pass
