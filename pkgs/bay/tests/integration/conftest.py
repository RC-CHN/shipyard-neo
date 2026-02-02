"""E2E API tests configuration and shared fixtures for Bay.

These tests require:
- Docker daemon running and accessible
- ship:latest image built and available
- Bay server running on http://localhost:8000

See: plans/phase-1/tests.md section 1
"""

from __future__ import annotations

import os
import subprocess

import httpx
import pytest

# Bay API base URL - can be overridden by E2E_BAY_PORT environment variable
_bay_port = os.environ.get("E2E_BAY_PORT", "8001")
BAY_BASE_URL = f"http://127.0.0.1:{_bay_port}"

# Test configuration
# API Key for E2E tests (must match config in tests/scripts/docker-host/config.yaml)
E2E_API_KEY = os.environ.get("E2E_API_KEY", "e2e-test-api-key")
AUTH_HEADERS = {"Authorization": f"Bearer {E2E_API_KEY}"}
DEFAULT_PROFILE = "python-default"


def is_bay_running() -> bool:
    """Check if Bay is running."""
    try:
        response = httpx.get(f"{BAY_BASE_URL}/health", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


def is_docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_ship_image_available() -> bool:
    """Check if ship:latest image exists."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "ship:latest"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def docker_volume_exists(volume_name: str) -> bool:
    """Check if a Docker volume exists."""
    try:
        result = subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def docker_container_exists(container_name: str) -> bool:
    """Check if a Docker container exists (running or stopped)."""
    try:
        result = subprocess.run(
            ["docker", "container", "inspect", container_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip all E2E tests if prerequisites not met
e2e_skipif_marks = [
    pytest.mark.skipif(
        not is_docker_available(),
        reason="Docker is not available",
    ),
    pytest.mark.skipif(
        not is_ship_image_available(),
        reason="ship:latest image not found. Run: cd pkgs/ship && make build",
    ),
    pytest.mark.skipif(
        not is_bay_running(),
        reason="Bay is not running. Start with: cd pkgs/bay && uv run python -m app.main",
    ),
]

# Combined pytest mark for E2E tests
pytestmark = e2e_skipif_marks

# pytest-xdist parallel execution configuration
# Most E2E tests can run in parallel because each creates/deletes its own sandbox.
# GC tests need serial execution to avoid interference with other sandboxes.
#
# Usage:
#   Parallel: pytest tests/integration -n auto --dist loadgroup
#   Serial:   pytest tests/integration (default)
#
# Tests marked with @pytest.mark.xdist_group("gc") will run in the same worker,
# effectively serializing them. Use this for GC tests.
gc_serial_mark = pytest.mark.xdist_group("gc")


# ---- GC Admin API helpers ----

import asyncio


async def trigger_gc(
    client: httpx.AsyncClient,
    *,
    max_retries: int = 10,
    retry_delay: float = 0.5
) -> dict:
    """Trigger a full GC cycle and wait for completion.

    Uses POST /v1/admin/gc/run to manually trigger GC.
    This is the recommended way to test GC behavior without relying on
    automatic timing.

    If GC is already running (HTTP 423), this function will retry with
    exponential backoff until the lock is released.

    Args:
        client: httpx.AsyncClient with auth headers configured
        max_retries: Maximum number of retries if GC is locked (default: 10)
        retry_delay: Initial delay between retries in seconds (default: 0.5)

    Returns:
        GC run response with results from each task

    Raises:
        AssertionError: If GC returns non-200 status after all retries

    Example:
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            result = await trigger_gc(client)
            assert result["total_cleaned"] >= 1
    """
    delay = retry_delay
    for attempt in range(max_retries + 1):
        try:
            # GC can take a long time if there are many resources to clean
            response = await client.post("/v1/admin/gc/run", timeout=120.0)
        except httpx.ReadTimeout:
            # GC is still running, wait and retry
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
                continue
            else:
                raise AssertionError(
                    f"GC timed out after {max_retries} retries"
                )
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 423:
            # GC is already running, wait and retry
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)  # Exponential backoff, max 5s
                continue
            else:
                raise AssertionError(
                    f"GC still locked after {max_retries} retries: {response.text}"
                )
        else:
            raise AssertionError(f"GC failed with status {response.status_code}: {response.text}")
    
    # Should not reach here, but just in case
    raise AssertionError("GC trigger failed unexpectedly")


async def trigger_gc_task(
    client: httpx.AsyncClient,
    task: str,
    *,
    max_retries: int = 10,
    retry_delay: float = 0.5
) -> dict:
    """Trigger a specific GC task and wait for completion.

    If GC is already running (HTTP 423), this function will retry with
    exponential backoff until the lock is released.

    Args:
        client: httpx.AsyncClient with auth headers configured
        task: Task name - one of:
            - "idle_session"
            - "expired_sandbox"
            - "orphan_workspace"
            - "orphan_container"
        max_retries: Maximum number of retries if GC is locked (default: 10)
        retry_delay: Initial delay between retries in seconds (default: 0.5)

    Returns:
        GC run response with results

    Example:
        result = await trigger_gc_task(client, "expired_sandbox")
        assert result["total_cleaned"] >= 1
    """
    delay = retry_delay
    for attempt in range(max_retries + 1):
        try:
            # GC can take a long time if there are many resources to clean
            response = await client.post("/v1/admin/gc/run", json={"tasks": [task]}, timeout=120.0)
        except httpx.ReadTimeout:
            # GC is still running, wait and retry
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
                continue
            else:
                raise AssertionError(
                    f"GC task {task} timed out after {max_retries} retries"
                )
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 423:
            # GC is already running, wait and retry
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)  # Exponential backoff, max 5s
                continue
            else:
                raise AssertionError(
                    f"GC task {task} still locked after {max_retries} retries: {response.text}"
                )
        else:
            raise AssertionError(f"GC task {task} failed with status {response.status_code}: {response.text}")
    
    # Should not reach here, but just in case
    raise AssertionError(f"GC task {task} trigger failed unexpectedly")
