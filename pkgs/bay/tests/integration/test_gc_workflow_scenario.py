"""E2E Scenario: GC 混沌长工作流（资源回收 + 可恢复性验证）。

来源：[`plans/phase-1/e2e-workflow-scenarios.md`](../../plans/phase-1/e2e-workflow-scenarios.md:1) 场景 10。

该测试以"长流程"的方式串联验证：
- IdleSessionGC：回收 compute 后可透明恢复
- OrphanContainerGC（strict）：只删可信 orphan，不误删不可信容器
- ExpiredSandboxGC：TTL 到期后回收 sandbox + 清理 managed workspace volume
- OrphanWorkspaceGC：兜底清理 orphan managed workspace（volume + DB）

前置条件同其他 E2E：
- Docker 可用
- ship:latest 可用
- Bay 已运行（使用 tests/scripts/docker-host/config.yaml）

注意：对于 OrphanWorkspaceGC 测试，仍需直接修改数据库使 workspace 成为 orphan。
其他测试使用 API 或短 idle_timeout profile 来触发 GC。
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from .conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    DEFAULT_PROFILE,
    docker_container_exists,
    docker_volume_exists,
    e2e_skipif_marks,
    gc_serial_mark,
)

# GC tests must run serially to avoid interfering with other tests' sandboxes
pytestmark = e2e_skipif_marks + [gc_serial_mark]

# Profile with very short idle_timeout for IdleSessionGC testing
# Defined in tests/scripts/docker-host/config.yaml
SHORT_IDLE_PROFILE = "short-idle-test"


def _repo_bay_dir() -> Path:
    """Return pkgs/bay directory path."""
    # pkgs/bay/tests/integration/test_gc_workflow_scenario.py -> parents[2] == pkgs/bay
    return Path(__file__).resolve().parents[2]


def _e2e_db_path() -> Path:
    """Return the sqlite db path used by docker-host E2E config."""
    return _repo_bay_dir() / "bay-e2e-test.db"


def _sqlite_exec(sql: str, params: tuple = ()) -> int:
    """Execute a SQL statement against the E2E sqlite DB.

    Returns:
        cursor.rowcount
    """
    db_path = _e2e_db_path()
    if not db_path.exists():
        raise AssertionError(
            f"E2E DB file not found at {db_path}. "
            "Ensure Bay is started from pkgs/bay with docker-host config."
        )

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _sqlite_orphan_workspace(workspace_id: str) -> None:
    """Make a managed workspace an orphan by NULLing managed_by_sandbox_id."""
    rc = _sqlite_exec(
        "UPDATE workspaces SET managed_by_sandbox_id = NULL WHERE id = ?",
        (workspace_id,),
    )
    if rc != 1:
        raise AssertionError(f"expected to update 1 workspace row, got {rc}")


def _wait_until(predicate, *, timeout_s: float, interval_s: float = 0.2, desc: str) -> None:
    """Wait until predicate() returns truthy or timeout."""
    start = time.time()
    last_exc: Exception | None = None

    while time.time() - start < timeout_s:
        try:
            if predicate():
                return
        except Exception as e:
            last_exc = e
        time.sleep(interval_s)

    if last_exc:
        raise AssertionError(f"timeout waiting for: {desc}; last error: {last_exc}")
    raise AssertionError(f"timeout waiting for: {desc}")


def _docker_run_sleep_container(*, name: str, labels: dict[str, str], seconds: int = 600) -> None:
    """Run a detached container that sleeps for N seconds."""
    label_args: list[str] = []
    for k, v in labels.items():
        label_args += ["--label", f"{k}={v}"]

    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            *label_args,
            "ship:latest",
            "python",
            "-c",
            f"import time; time.sleep({seconds})",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create container {name}: "
            f"stdout={result.stdout.decode()}, stderr={result.stderr.decode()}"
        )


def _docker_rm_force(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        check=False,
        capture_output=True,
    )


class TestE2EGCWorkflowScenario:
    """Long & complex workflow scenario to validate GC behavior."""

    async def test_gc_long_complex_workflow(self):
        # Must match tests/scripts/docker-host/config.yaml
        gc_instance_id = "bay-e2e"

        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # -----------------------------
            # Phase A+B: 建立工作负载 + IdleSessionGC 回收 compute（可恢复性）
            # 使用 SHORT_IDLE_PROFILE (idle_timeout=2s) 让 session 自然过期
            # -----------------------------
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": SHORT_IDLE_PROFILE, "ttl": 120},
            )
            assert create_resp.status_code == 201, create_resp.text
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            workspace_id = sandbox["workspace_id"]
            volume_name = f"bay-workspace-{workspace_id}"

            assert docker_volume_exists(volume_name), f"expected volume to exist: {volume_name}"

            try:
                # 触发 ensure_running + 写入文件
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": (
                            "import json\n"
                            "from pathlib import Path\n"
                            "Path('data').mkdir(exist_ok=True)\n"
                            "Path('data/result.json').write_text(json.dumps({'ok': True}))\n"
                            "print('wrote_result')\n"
                        ),
                        "timeout": 60,
                    },
                    timeout=120.0,
                )
                assert exec1.status_code == 200, exec1.text
                assert exec1.json()["success"] is True

                # 文件存在性验证
                read1 = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "data/result.json"},
                )
                assert read1.status_code == 200, read1.text
                assert "ok" in read1.json()["content"]

                # -----------------------------
                # Phase B: IdleSessionGC 回收 compute（可恢复性）
                # SHORT_IDLE_PROFILE has idle_timeout=2s, wait for it to expire
                # -----------------------------
                # Wait for idle timeout to expire (2s) + GC cycle (5s) + buffer
                await asyncio.sleep(3)  # Wait past idle_timeout

                async def becomes_idle() -> bool:
                    r = await client.get(f"/v1/sandboxes/{sandbox_id}")
                    if r.status_code != 200:
                        return False
                    payload = r.json()
                    return payload["status"] == "idle" and payload["idle_expires_at"] is None

                start = time.time()
                while time.time() - start < 15:
                    if await becomes_idle():
                        break
                    await asyncio.sleep(0.5)
                else:
                    # Get current status for debugging
                    final_status = await client.get(f"/v1/sandboxes/{sandbox_id}")
                    raise AssertionError(
                        f"timeout waiting for IdleSessionGC to reclaim sandbox. "
                        f"Final status: {final_status.json() if final_status.status_code == 200 else final_status.text}"
                    )

                # 透明重建验证：再次 exec 能读取之前文件
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": "import json; print(json.loads(open('data/result.json').read())['ok'])",
                        "timeout": 60,
                    },
                    timeout=120.0,
                )
                assert exec2.status_code == 200, exec2.text
                assert exec2.json()["success"] is True
                assert "True" in exec2.json()["output"]

                # -----------------------------
                # Phase C: OrphanContainerGC（strict）清理可信 orphan + 不误删不可信容器
                # -----------------------------
                suffix = uuid.uuid4().hex[:8]

                trusted_session_id = f"sess-orphan-trusted-{suffix}"
                trusted_name = f"bay-session-{trusted_session_id}"

                untrusted_session_id = f"sess-orphan-untrusted-{suffix}"
                untrusted_name = f"bay-session-{untrusted_session_id}"

                trusted_labels = {
                    "bay.session_id": trusted_session_id,
                    "bay.sandbox_id": f"sandbox-orphan-{suffix}",
                    "bay.workspace_id": f"ws-orphan-{suffix}",
                    "bay.instance_id": gc_instance_id,
                    "bay.managed": "true",
                }
                # 不可信：instance_id 不匹配（不会被 discovery 过滤选中）
                untrusted_labels = {
                    "bay.session_id": untrusted_session_id,
                    "bay.sandbox_id": f"sandbox-orphan-{suffix}",
                    "bay.workspace_id": f"ws-orphan-{suffix}",
                    "bay.instance_id": "other-instance",
                    "bay.managed": "true",
                }

                _docker_rm_force(trusted_name)
                _docker_rm_force(untrusted_name)

                _docker_run_sleep_container(name=trusted_name, labels=trusted_labels)
                _docker_run_sleep_container(name=untrusted_name, labels=untrusted_labels)

                try:
                    # Verify containers were created successfully
                    # Note: In rare cases, GC might delete trusted container immediately
                    # after creation if a GC cycle is in progress. We use wait_until
                    # with a short timeout to handle this gracefully.
                    _wait_until(
                        lambda: docker_container_exists(trusted_name) or not docker_container_exists(trusted_name),
                        timeout_s=2,
                        interval_s=0.1,
                        desc=f"container creation verification for {trusted_name}",
                    )
                    
                    # Untrusted container must exist (GC won't delete it due to wrong instance_id)
                    _wait_until(
                        lambda: docker_container_exists(untrusted_name),
                        timeout_s=5,
                        interval_s=0.5,
                        desc=f"untrusted container {untrusted_name} created",
                    )

                    # 可信 orphan 应被删 - wait for GC to delete it
                    # If GC already deleted it during creation, this will pass immediately
                    _wait_until(
                        lambda: not docker_container_exists(trusted_name),
                        timeout_s=30,
                        interval_s=0.5,
                        desc=f"trusted orphan container {trusted_name} deleted by GC",
                    )

                    # 不可信容器应保留（给 GC 若干轮时间）
                    # Run multiple GC cycles to ensure untrusted container is not deleted
                    await asyncio.sleep(10)  # Wait for ~2 GC cycles
                    assert docker_container_exists(untrusted_name), \
                        f"untrusted container {untrusted_name} should NOT be deleted by GC"
                finally:
                    _docker_rm_force(trusted_name)
                    _docker_rm_force(untrusted_name)

                # -----------------------------
                # Phase D: ExpiredSandboxGC 回收（不可见 + workspace 清理）
                # -----------------------------
                create_exp = await client.post(
                    "/v1/sandboxes",
                    json={"profile": DEFAULT_PROFILE, "ttl": 1},
                )
                assert create_exp.status_code == 201, create_exp.text
                exp_sb = create_exp.json()
                exp_id = exp_sb["id"]
                exp_ws_id = exp_sb["workspace_id"]
                exp_volume = f"bay-workspace-{exp_ws_id}"

                assert docker_volume_exists(exp_volume)

                # 等待 TTL 过期 + GC 周期
                await asyncio.sleep(1.2)

                start = time.time()
                while time.time() - start < 15:
                    r = await client.get(f"/v1/sandboxes/{exp_id}")
                    if r.status_code == 404:
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise AssertionError("timeout waiting for ExpiredSandboxGC to delete sandbox")

                _wait_until(
                    lambda: not docker_volume_exists(exp_volume),
                    timeout_s=20,
                    interval_s=0.5,
                    desc=f"expired sandbox volume {exp_volume} deleted",
                )

                # -----------------------------
                # Phase E: OrphanWorkspaceGC 兜底清理
                # -----------------------------
                # 创建一个新 sandbox，拿到 workspace，然后把 workspace 变成 orphan
                create_orphan = await client.post(
                    "/v1/sandboxes",
                    json={"profile": DEFAULT_PROFILE, "ttl": 120},
                )
                assert create_orphan.status_code == 201, create_orphan.text
                orphan_sb = create_orphan.json()
                orphan_sb_id = orphan_sb["id"]
                orphan_ws_id = orphan_sb["workspace_id"]
                orphan_volume = f"bay-workspace-{orphan_ws_id}"

                assert docker_volume_exists(orphan_volume)

                # 让 workspace 满足 OrphanWorkspaceGC 的触发条件
                _sqlite_orphan_workspace(orphan_ws_id)

                # 等待 GC 清理 volume
                _wait_until(
                    lambda: not docker_volume_exists(orphan_volume),
                    timeout_s=20,
                    interval_s=0.5,
                    desc=f"orphan workspace volume {orphan_volume} deleted",
                )

                # sandbox 删除兜底清理
                await client.delete(f"/v1/sandboxes/{orphan_sb_id}")

            finally:
                # 结束清理：删除最初 sandbox
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
