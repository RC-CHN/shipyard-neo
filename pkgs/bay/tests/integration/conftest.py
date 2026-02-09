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

# Driver type detection: 'docker' (default) or 'k8s'
E2E_DRIVER_TYPE = os.environ.get("E2E_DRIVER_TYPE", "docker")
E2E_K8S_NAMESPACE = os.environ.get("E2E_K8S_NAMESPACE", "bay-e2e-test")

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
    # Resilience tests - Phase 1.5: Container crash and GC race condition tests
    # need serial execution to avoid interference with other tests
    "resilience": [
        r"resilience/test_container_crash\.py::",
        r"resilience/test_gc_race_condition\.py::",
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
# K8S HELPERS
# =============================================================================

def k8s_pvc_exists(name: str, namespace: str | None = None) -> bool:
    """Check if a PersistentVolumeClaim exists in K8s."""
    ns = namespace or E2E_K8S_NAMESPACE
    try:
        return subprocess.run(
            ["kubectl", "get", "pvc", name, "-n", ns],
            capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False


def k8s_get_pod_by_label(label_key: str, label_value: str, namespace: str | None = None) -> str | None:
    """Get Pod name by label. Returns first matching pod name or None."""
    ns = namespace or E2E_K8S_NAMESPACE
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pods",
                "-n", ns,
                "-l", f"{label_key}={label_value}",
                "-o", "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except Exception:
        return None


def k8s_delete_pod(pod_name: str, namespace: str | None = None, force: bool = False) -> bool:
    """Delete a Pod. Returns True if succeeded."""
    ns = namespace or E2E_K8S_NAMESPACE
    cmd = ["kubectl", "delete", "pod", pod_name, "-n", ns]
    if force:
        cmd.extend(["--grace-period=0", "--force"])
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def k8s_get_pod_exit_code(pod_name: str, namespace: str | None = None) -> int | None:
    """Get Pod container exit code. Returns None if not available."""
    ns = namespace or E2E_K8S_NAMESPACE
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pod", pod_name,
                "-n", ns,
                "-o", "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
        return None
    except Exception:
        return None


def k8s_get_pod_uid(pod_name: str, namespace: str | None = None) -> str | None:
    """Get Pod UID (changes when a Pod is recreated with the same name)."""
    ns = namespace or E2E_K8S_NAMESPACE
    try:
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                pod_name,
                "-n",
                ns,
                "-o",
                "jsonpath={.metadata.uid}",
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except Exception:
        return None


# =============================================================================
# UNIFIED STORAGE HELPERS
# =============================================================================

def cargo_volume_exists(cargo_id: str) -> bool:
    """Check if a cargo's underlying volume/PVC exists.
    
    Automatically detects driver type from E2E_DRIVER_TYPE environment variable
    and uses appropriate check method.
    
    Args:
        cargo_id: The cargo ID (e.g., 'ws-abc123')
    
    Returns:
        True if the volume/PVC exists, False otherwise
    """
    volume_name = f"bay-cargo-{cargo_id}"
    
    if E2E_DRIVER_TYPE == "k8s":
        return k8s_pvc_exists(volume_name)
    else:
        return docker_volume_exists(volume_name)


# =============================================================================
# UNIFIED CONTAINER/POD HELPERS
# =============================================================================

def get_runtime_id_by_sandbox(sandbox_id: str) -> str | None:
    """Get container ID (Docker) or Pod name (K8s) for a sandbox.
    
    Returns the first matching runtime instance or None if not found.
    """
    if E2E_DRIVER_TYPE == "k8s":
        return k8s_get_pod_by_label("bay.sandbox_id", sandbox_id)
    else:
        # Docker mode
        try:
            result = subprocess.run(
                [
                    "docker", "ps", "-q",
                    "--filter", f"label=bay.sandbox_id={sandbox_id}",
                ],
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split("\n")[0]
            return None
        except Exception:
            return None


def kill_runtime(runtime_id: str) -> bool:
    """Force kill a container (Docker) or delete a Pod (K8s).
    
    Returns True if the runtime was killed or is already dead/gone.
    In parallel test environments, the container may have already been
    removed by another test's cleanup or by Bay's health probing before
    we get to kill it — that still counts as "killed" for our purposes.
    """
    if E2E_DRIVER_TYPE == "k8s":
        return k8s_delete_pod(runtime_id, force=True)
    else:
        # Docker mode
        try:
            result = subprocess.run(
                ["docker", "kill", runtime_id],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
            # docker kill failed — container may already be dead or removed.
            # Check if the container still exists but is not running (already exited).
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", runtime_id],
                capture_output=True,
                timeout=5,
                text=True,
            )
            if inspect.returncode != 0:
                # Container doesn't exist at all — already removed, treat as killed
                return True
            if inspect.stdout.strip() == "false":
                # Container exists but is stopped — already dead, treat as killed
                return True
            return False
        except Exception:
            return False


def get_runtime_exit_code(runtime_id: str) -> int | None:
    """Get container/Pod exit code. Returns None if not available."""
    if E2E_DRIVER_TYPE == "k8s":
        return k8s_get_pod_exit_code(runtime_id)
    else:
        # Docker mode
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.ExitCode}}", runtime_id],
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return int(result.stdout.strip())
            return None
        except Exception:
            return None


def get_runtime_identity(runtime_id: str) -> str | None:
    """Get a stable identity token for the runtime instance.

    - Docker: container ID is already unique/stable for the lifetime of the container.
    - K8s: Pod name can be reused; use Pod UID to detect recreation.

    Returns None if the identity can't be determined.
    """
    if E2E_DRIVER_TYPE == "k8s":
        return k8s_get_pod_uid(runtime_id)
    return runtime_id



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
        except httpx.TransportError as e:
            # In some failure modes (e.g. Bay pod restart / port-forward drop),
            # the server may disconnect during cleanup. Don't mask the test result.
            import warnings

            warnings.warn(
                f"Transport error deleting sandbox {sandbox['id']} during cleanup: {e}",
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
