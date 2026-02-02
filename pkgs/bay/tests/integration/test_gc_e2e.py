"""E2E: GC (Garbage Collection) tests.

These tests verify that the background GC loop actually reclaims resources.

Prerequisites (same as other E2E tests):
- Docker daemon running
- ship:latest image available
- Bay server running with BAY_CONFIG_FILE=tests/scripts/docker-host/config.yaml
  (this repo patch enables gc in that config, with short interval_seconds).

Notes on strategy:
- We test GC effects via public API + Docker inspection.
- For IdleSessionGC we need to force idle_expires_at into the past; we do this
  by directly updating the SQLite db file used by the E2E Bay server.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta
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
)

pytestmark = e2e_skipif_marks


def _repo_bay_dir() -> Path:
    """Return pkgs/bay directory path."""
    # tests/integration/test_gc_e2e.py -> parents[2] == pkgs/bay
    return Path(__file__).resolve().parents[2]


def _e2e_db_path() -> Path:
    """Return the sqlite db path used by docker-host E2E config."""
    # Must match pkgs/bay/tests/scripts/docker-host/config.yaml
    return _repo_bay_dir() / "bay-e2e-test.db"


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


def _sqlite_update_idle_expires_at(sandbox_id: str, *, idle_expires_at: datetime | None) -> None:
    """Force-update sandboxes.idle_expires_at in the E2E sqlite db."""
    db_path = _e2e_db_path()
    if not db_path.exists():
        raise AssertionError(
            f"E2E DB file not found at {db_path}. "
            "Ensure Bay is started from pkgs/bay with docker-host config."
        )

    # SQLModel stores datetime as ISO string in SQLite by default.
    value = idle_expires_at.isoformat() if idle_expires_at is not None else None

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sandboxes SET idle_expires_at = ? WHERE id = ?",
            (value, sandbox_id),
        )
        conn.commit()

        if cur.rowcount != 1:
            raise AssertionError(f"expected to update 1 row, got {cur.rowcount}")
    finally:
        conn.close()


class TestE2EGC:
    """E2E tests for GC behavior."""

    async def test_expired_sandbox_gc_deletes_sandbox_and_workspace(self):
        """ExpiredSandboxGC should soft-delete the sandbox and delete managed workspace volume."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with very short TTL
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 1},
            )
            assert create_resp.status_code == 201, create_resp.text
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            workspace_id = sandbox["workspace_id"]
            volume_name = f"bay-workspace-{workspace_id}"

            # Volume should exist immediately after create
            assert docker_volume_exists(volume_name), f"expected volume to exist: {volume_name}"

            # Wait for TTL to expire
            time.sleep(1.2)

            # GC interval is 1s in E2E config; allow a few seconds
            async def sandbox_is_gone() -> bool:
                r = await client.get(f"/v1/sandboxes/{sandbox_id}")
                return r.status_code == 404

            # Poll until sandbox is soft-deleted (API returns 404)
            start = time.time()
            while time.time() - start < 10:
                r = await client.get(f"/v1/sandboxes/{sandbox_id}")
                if r.status_code == 404:
                    break
                await asyncio_sleep(0.2)
            else:
                raise AssertionError("timeout waiting for expired sandbox to be deleted by GC")

            # Volume should be deleted by cascade delete
            _wait_until(
                lambda: not docker_volume_exists(volume_name),
                timeout_s=10,
                interval_s=0.5,
                desc=f"volume {volume_name} deleted by GC",
            )

    async def test_idle_session_gc_reclaims_compute_and_allows_recreate(self):
        """IdleSessionGC should destroy sessions and clear idle_expires_at/current_session_id."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_resp.status_code == 201, create_resp.text
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]

            try:
                # Trigger ensure_running by executing python
                exec_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print('hello')", "timeout": 30},
                    timeout=120.0,
                )
                assert exec_resp.status_code == 200, exec_resp.text

                # Force idle_expires_at into the past
                _sqlite_update_idle_expires_at(
                    sandbox_id,
                    idle_expires_at=datetime.utcnow() - timedelta(minutes=1),
                )

                # Wait for GC to clear the session (status becomes idle)
                async def becomes_idle() -> bool:
                    r = await client.get(f"/v1/sandboxes/{sandbox_id}")
                    if r.status_code != 200:
                        return False
                    payload = r.json()
                    return payload["status"] == "idle" and payload["idle_expires_at"] is None

                start = time.time()
                while time.time() - start < 10:
                    if await becomes_idle():
                        break
                    await asyncio_sleep(0.2)
                else:
                    raise AssertionError("timeout waiting for IdleSessionGC to reclaim sandbox")

                # After GC, another exec should recreate compute successfully
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(1+1)", "timeout": 30},
                    timeout=120.0,
                )
                assert exec2.status_code == 200, exec2.text
                assert exec2.json()["success"] is True

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_orphan_container_gc_deletes_trusted_orphan(self):
        """OrphanContainerGC (strict) should delete a trusted container without DB session."""
        # Must match tests/scripts/docker-host/config.yaml
        instance_id = "bay-e2e"

        orphan_suffix = uuid.uuid4().hex[:8]
        session_id = f"sess-orphan-{orphan_suffix}"
        container_name = f"bay-session-{session_id}"

        # Create a long-running container using ship:latest (python should exist)
        # Labels must satisfy OrphanContainerGC strict checks.
        labels = {
            "bay.session_id": session_id,
            "bay.sandbox_id": f"sandbox-orphan-{orphan_suffix}",
            "bay.workspace_id": f"ws-orphan-{orphan_suffix}",
            "bay.instance_id": instance_id,
            "bay.managed": "true",
        }

        label_args = []
        for k, v in labels.items():
            label_args += ["--label", f"{k}={v}"]

        # Create container
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                *label_args,
                "ship:latest",
                "python",
                "-c",
                "import time; time.sleep(600)",
            ],
            check=True,
            capture_output=True,
        )

        try:
            assert docker_container_exists(container_name), "container should exist before GC"

            # Wait for GC to delete the container
            _wait_until(
                lambda: not docker_container_exists(container_name),
                timeout_s=20,
                interval_s=0.5,
                desc=f"orphan container {container_name} deleted by OrphanContainerGC",
            )

        finally:
            # Cleanup in case GC didn't delete (or test failed early)
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                check=False,
                capture_output=True,
            )


async def asyncio_sleep(seconds: float) -> None:
    """Async sleep helper (avoid importing asyncio at module import time)."""
    import asyncio

    await asyncio.sleep(seconds)
