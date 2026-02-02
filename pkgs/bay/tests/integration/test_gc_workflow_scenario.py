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

策略：
- 使用 POST /v1/admin/gc/run 手动触发 GC 而非等待自动 GC
- 这提供了确定性的测试行为，不依赖时序
- 对于 OrphanWorkspaceGC 测试，仍需直接修改数据库使 workspace 成为 orphan
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
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
    trigger_gc,
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
    """Long & complex workflow scenario to validate GC behavior.
    
    Uses Admin API to trigger GC manually for deterministic testing.
    """

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
                # Then trigger GC manually
                # -----------------------------
                await asyncio.sleep(3)  # Wait past idle_timeout (2s) + buffer

                # Trigger GC manually
                gc_result = await trigger_gc(client)
                
                # Verify idle_session task ran and cleaned
                idle_result = next(
                    (r for r in gc_result["results"] if r["task_name"] == "idle_session"),
                    None,
                )
                assert idle_result is not None, "idle_session task should have run"
                assert idle_result["cleaned_count"] >= 1, f"Expected at least 1 session cleaned, got {idle_result}"

                # Verify sandbox is now idle
                status_resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert status_resp.status_code == 200
                status_data = status_resp.json()
                assert status_data["status"] == "idle", f"Expected idle, got {status_data}"
                assert status_data["idle_expires_at"] is None

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
                # 不可信：instance_id 不匹配（不会被 GC 删除）
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
                    # Verify containers were created
                    assert docker_container_exists(trusted_name), f"trusted container {trusted_name} should exist"
                    assert docker_container_exists(untrusted_name), f"untrusted container {untrusted_name} should exist"

                    # Trigger GC manually
                    gc_result = await trigger_gc(client)
                    
                    # Verify orphan_container task ran
                    orphan_result = next(
                        (r for r in gc_result["results"] if r["task_name"] == "orphan_container"),
                        None,
                    )
                    assert orphan_result is not None, "orphan_container task should have run"
                    assert orphan_result["cleaned_count"] >= 1, f"Expected at least 1 container cleaned, got {orphan_result}"

                    # 可信 orphan 应被删
                    assert not docker_container_exists(trusted_name), \
                        f"trusted orphan container {trusted_name} should be deleted by GC"

                    # 不可信容器应保留
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

                # 等待 TTL 过期
                await asyncio.sleep(1.2)

                # Verify sandbox status is EXPIRED before GC
                status_resp = await client.get(f"/v1/sandboxes/{exp_id}")
                assert status_resp.status_code == 200
                assert status_resp.json()["status"] == "expired", "Sandbox should be expired before GC"

                # Trigger GC manually
                gc_result = await trigger_gc(client)
                
                # Verify expired_sandbox task ran
                expired_result = next(
                    (r for r in gc_result["results"] if r["task_name"] == "expired_sandbox"),
                    None,
                )
                assert expired_result is not None, "expired_sandbox task should have run"
                assert expired_result["cleaned_count"] >= 1, f"Expected at least 1 sandbox cleaned, got {expired_result}"

                # Sandbox should be deleted
                final_resp = await client.get(f"/v1/sandboxes/{exp_id}")
                assert final_resp.status_code == 404, "Expired sandbox should be deleted"

                # Volume should be deleted
                assert not docker_volume_exists(exp_volume), f"Volume {exp_volume} should be deleted"

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

                # Trigger GC manually
                gc_result = await trigger_gc(client)
                
                # Verify orphan_workspace task ran
                orphan_ws_result = next(
                    (r for r in gc_result["results"] if r["task_name"] == "orphan_workspace"),
                    None,
                )
                assert orphan_ws_result is not None, "orphan_workspace task should have run"
                assert orphan_ws_result["cleaned_count"] >= 1, f"Expected at least 1 workspace cleaned, got {orphan_ws_result}"

                # Volume should be deleted
                assert not docker_volume_exists(orphan_volume), f"Orphan volume {orphan_volume} should be deleted"

                # sandbox 删除兜底清理
                await client.delete(f"/v1/sandboxes/{orphan_sb_id}")

            finally:
                # 结束清理：删除最初 sandbox
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
