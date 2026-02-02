"""GC: IdleSessionGC invariants.

原则：不依赖 cleaned_count，只断言最终状态不变量。
并且尽量避免固定 sleep；用轮询等待“时序条件已满足”，再用 Admin API 手动触发 GC。

实现对照：[`IdleSessionGC.run()`](pkgs/bay/app/services/gc/tasks/idle_session.py:51)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from tests.integration.conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    create_sandbox,
    e2e_skipif_marks,
    trigger_gc,
)

pytestmark = e2e_skipif_marks

# Profile with very short idle_timeout for IdleSessionGC testing.
# Expected to be provided by tests/scripts/docker-host/config.yaml
SHORT_IDLE_PROFILE = "short-idle-test"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Pydantic may return ISO strings with or without timezone.
    s = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _wait_until(
    *,
    predicate,
    timeout_s: float,
    interval_s: float = 0.2,
) -> None:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        ok, last = await predicate()
        if ok:
            return
        await asyncio_sleep(interval_s)
    raise AssertionError(f"condition not met within {timeout_s}s (last={last})")


async def asyncio_sleep(seconds: float) -> None:
    # Local wrapper to keep imports minimal inside test file.
    import asyncio

    await asyncio.sleep(seconds)


class TestIdleSessionGC:
    async def test_idle_session_gc_reclaims_compute_but_preserves_workspace(self) -> None:
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            async with create_sandbox(client, profile=SHORT_IDLE_PROFILE, ttl=120) as sandbox:
                sandbox_id = sandbox["id"]

                # 1) Start compute and write a file into the workspace.
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

                # 2) Wait until IdleSessionGC condition becomes true:
                # idle_expires_at is set and already in the past.
                async def idle_condition():
                    resp = await client.get(f"/v1/sandboxes/{sandbox_id}")
                    assert resp.status_code == 200, resp.text
                    data = resp.json()
                    dt = _parse_dt(data.get("idle_expires_at"))
                    now = datetime.now(timezone.utc)
                    return (dt is not None and dt < now), {
                        "status": data.get("status"),
                        "idle_expires_at": data.get("idle_expires_at"),
                    }

                await _wait_until(predicate=idle_condition, timeout_s=15.0)

                # 3) Trigger ONLY idle_session via Admin API.
                await trigger_gc(client, tasks=["idle_session"])

                # 4) Invariant: sandbox is idle and idle_expires_at cleared.
                status = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert status.status_code == 200, status.text
                data = status.json()
                assert data["status"] == "idle", f"expected idle, got: {data}"
                assert data["idle_expires_at"] is None

                # 5) Invariant: compute can be recreated and /workspace persists.
                code = (
                    "import json; "
                    "print(json.loads(open('data/result.json').read())['ok'])"
                )
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": code,
                        "timeout": 60,
                    },
                    timeout=120.0,
                )
                assert exec2.status_code == 200, exec2.text
                assert exec2.json()["success"] is True
                assert "True" in exec2.json().get("output", "")
