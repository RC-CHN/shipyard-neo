"""E2E integration tests configuration for Bay.

Prerequisites:
- Docker daemon running
- ship:latest image available
- Bay server running

## Execution Groups

Tests use pytest-xdist for parallel execution:

  pytest tests/integration -n auto --dist loadgroup

Group assignment is centralized here via pytest_collection_modifyitems hook.
Do NOT use @xdist_group decorators in test files - add patterns to SERIAL_GROUPS.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncGenerator

import httpx
import pytest

if TYPE_CHECKING:
    from _pytest.config import Config
    from _pytest.python import Function


# =============================================================================
# CONFIGURATION
# =============================================================================

BAY_BASE_URL = f"http://127.0.0.1:{os.environ.get('E2E_BAY_PORT', '8001')}"
E2E_API_KEY = os.environ.get("E2E_API_KEY", "e2e-test-api-key")
AUTH_HEADERS = {"Authorization": f"Bearer {E2E_API_KEY}"}
DEFAULT_PROFILE = "python-default"

# Timeout configuration for parallel test stability
# Docker operations (create/delete container/volume) can be slow under load
DEFAULT_TIMEOUT = 30.0  # Default timeout for most operations
CLEANUP_TIMEOUT = 60.0  # Longer timeout for cleanup (delete) operations
EXEC_TIMEOUT = 120.0    # Timeout for code execution


# =============================================================================
# SERIAL TEST GROUPS - Tests that must run serially
# =============================================================================
#
# 目标：实现“两阶段一口气跑完”
# - Phase 1（并行）：跑所有未被标记为 serial 的测试（-n auto）
# - Phase 2（串行/独占）：跑所有 serial 测试（-n 1，确保 Bay 上同一时间只有一个测试在跑）
#
# SERIAL_GROUPS 的作用：
# - 给必须串行的测试按语义分组（timing/gc/workflows/…）
# - collection 时自动打上：
#   - pytest.mark.serial
#   - pytest.mark.serial_group("<group>")
#   - pytest.mark.xdist_group("<group>")
#
# 注意：测试文件内不要手写 xdist_group/serial 标记，统一在这里管理。

SERIAL_GROUPS = {
    # Timing-sensitive tests - TTL expiration depends on wall clock
    "timing": [
        r"core/test_extend_ttl\.py::test_extend_ttl_rejects_expired",
        r"test_long_running_extend_ttl\.py::",
    ],
    # GC tests - must be exclusive (Phase 2, -n 1)
    "gc": [
        r"/gc/",  # All tests in integration/gc/ directory
        r"test_gc_.*\.py::",
    ],
    # Workflow tests - scenario-style tests, prefer serial execution
    "workflows": [
        r"workflows/",
        # Back-compat: legacy workflow-style tests still in root
        r"test_.*workflow.*\.py::",
        r"test_mega_workflow\.py::",
        r"test_interactive_workflow\.py::",
        r"test_agent_coding_workflow\.py::",
        r"test_script_development\.py::",
        r"test_project_init\.py::",
        r"test_serverless_execution\.py::",
    ],
}

_COMPILED_GROUPS: dict[str, list[re.Pattern]] = {
    group: [re.compile(p) for p in patterns]
    for group, patterns in SERIAL_GROUPS.items()
}


def pytest_configure(config: Config) -> None:
    """Register custom markers for clarity in `pytest --markers` / CI."""
    config.addinivalue_line(
        "markers",
        "serial: run in Phase 2 with -n 1 to ensure exclusive execution against Bay",
    )
    config.addinivalue_line(
        "markers",
        "serial_group(name): semantic serial group name (timing/gc/workflows/...)",
    )


def pytest_collection_modifyitems(config: Config, items: list[Function]) -> None:
    """Assign markers based on SERIAL_GROUPS.

    - Matched tests: serial + serial_group(<name>) + xdist_group(<name>)
    - Unmatched tests: remain parallel-eligible (Phase 1)
    """
    for item in items:
        for group, patterns in _COMPILED_GROUPS.items():
            if any(p.search(item.nodeid) for p in patterns):
                item.add_marker(pytest.mark.serial)
                item.add_marker(pytest.mark.serial_group(group))
                item.add_marker(pytest.mark.xdist_group(group))
                break


# =============================================================================
# ENVIRONMENT CHECKS
# =============================================================================

def _check_docker() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _check_ship_image() -> bool:
    try:
        return subprocess.run(
            ["docker", "image", "inspect", "ship:latest"],
            capture_output=True, timeout=5
        ).returncode == 0
    except Exception:
        return False


def _check_bay() -> bool:
    try:
        return httpx.get(f"{BAY_BASE_URL}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


e2e_skipif_marks = [
    pytest.mark.skipif(not _check_docker(), reason="Docker unavailable"),
    pytest.mark.skipif(not _check_ship_image(), reason="ship:latest not found"),
    pytest.mark.skipif(not _check_bay(), reason="Bay not running"),
]

pytestmark = e2e_skipif_marks


# =============================================================================
# DOCKER HELPERS
# =============================================================================

def docker_volume_exists(name: str) -> bool:
    try:
        return subprocess.run(
            ["docker", "volume", "inspect", name],
            capture_output=True, timeout=5
        ).returncode == 0
    except Exception:
        return False


def docker_container_exists(name: str) -> bool:
    try:
        return subprocess.run(
            ["docker", "container", "inspect", name],
            capture_output=True, timeout=5
        ).returncode == 0
    except Exception:
        return False


# =============================================================================
# SANDBOX FIXTURES
# =============================================================================

@asynccontextmanager
async def create_sandbox(
    client: httpx.AsyncClient,
    *,
    profile: str = DEFAULT_PROFILE,
    ttl: int | None = None,
) -> AsyncGenerator[dict, None]:
    """Create sandbox with auto-cleanup.
    
    Cleanup uses a longer timeout to handle parallel test load.
    Timeout errors during cleanup are logged but not raised to avoid
    masking actual test failures.
    """
    body: dict = {"profile": profile}
    if ttl is not None:
        body["ttl"] = ttl

    resp = await client.post("/v1/sandboxes", json=body, timeout=DEFAULT_TIMEOUT)
    assert resp.status_code == 201, f"Create failed: {resp.text}"
    sandbox = resp.json()

    try:
        yield sandbox
    finally:
        try:
            await client.delete(
                f"/v1/sandboxes/{sandbox['id']}",
                timeout=CLEANUP_TIMEOUT,
            )
        except httpx.TimeoutException:
            # Log but don't fail - cleanup will happen via GC or manual cleanup
            import warnings
            warnings.warn(
                f"Timeout deleting sandbox {sandbox['id']} during cleanup. "
                "Sandbox will be cleaned up by GC or manual cleanup script.",
                stacklevel=2,
            )


# =============================================================================
# GC HELPERS
# =============================================================================

async def trigger_gc(
    client: httpx.AsyncClient,
    *,
    tasks: list[str] | None = None,
    max_retries: int = 10,
    retry_delay: float = 0.5,
) -> dict:
    """Trigger GC with retry on lock.

    Args:
        tasks: Specific tasks to run, or None for full GC.
               Options: idle_session, expired_sandbox, orphan_cargo, orphan_container
    """
    body = {"tasks": tasks} if tasks else None
    delay = retry_delay

    for attempt in range(max_retries + 1):
        try:
            resp = await client.post("/v1/admin/gc/run", json=body, timeout=120.0)
        except httpx.ReadTimeout:
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
                continue
            raise AssertionError(f"GC timed out after {max_retries} retries")

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 423:
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
                continue
            raise AssertionError(f"GC locked after {max_retries} retries")
        else:
            raise AssertionError(f"GC failed: {resp.status_code} {resp.text}")

    raise AssertionError("GC failed unexpectedly")
