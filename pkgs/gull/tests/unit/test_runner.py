"""Unit tests for Gull command runner.

Focuses on the internal `_run_agent_browser()` helper:
- Injects `--session` and `--profile`
- Uses `shlex.split()` so quoted args are preserved
- Properly returns stdout/stderr/exit_code

These tests do NOT require agent-browser to be installed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import app.main as gull_main


@dataclass
class _FakeProcess:
    stdout_bytes: bytes
    stderr_bytes: bytes
    returncode: int = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout_bytes, self.stderr_bytes

    def kill(self) -> None:
        # emulate subprocess API
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_run_agent_browser_injects_session_and_profile(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return _FakeProcess(b"out", b"err", 0)

    monkeypatch.setattr(
        gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    stdout, stderr, code = await gull_main._run_agent_browser(
        "open https://example.com",
        session="sess-1",
        profile="/workspace/.browser/profile",
        timeout=10,
        cwd="/workspace",
    )

    assert stdout == "out"
    assert stderr == "err"
    assert code == 0

    argv = captured["args"]
    assert argv[:1] == ["agent-browser"]
    assert "--session" in argv
    assert "sess-1" in argv
    assert "--profile" in argv
    assert "/workspace/.browser/profile" in argv

    # Ensure we keep working directory
    assert captured["kwargs"]["cwd"] == "/workspace"


@pytest.mark.asyncio
async def test_run_agent_browser_preserves_quoted_args(monkeypatch: pytest.MonkeyPatch):
    captured_argv: list[str] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal captured_argv
        captured_argv = list(args)
        return _FakeProcess(b"", b"", 0)

    monkeypatch.setattr(
        gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    await gull_main._run_agent_browser(
        'fill @e1 "hello world"',
        session="s",
        profile="/p",
        timeout=10,
    )

    # The quoted string should remain one argument
    assert "fill" in captured_argv
    assert "@e1" in captured_argv
    assert "hello world" in captured_argv


@pytest.mark.asyncio
async def test_run_agent_browser_timeout_kills_process(monkeypatch: pytest.MonkeyPatch):
    class _SlowProcess(_FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"late", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _SlowProcess(b"", b"", 0)

    monkeypatch.setattr(
        gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    stdout, stderr, code = await gull_main._run_agent_browser(
        "snapshot -i",
        session="s",
        profile="/p",
        timeout=0.01,
    )

    assert stdout == ""
    assert "timed out" in stderr
    assert code == -1


@pytest.mark.asyncio
async def test_exec_batch_stops_when_budget_exhausted_before_next_step(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_timeouts: list[float] = []

    async def fake_run(_cmd: str, **kwargs):
        captured_timeouts.append(kwargs["timeout"])
        return "ok", "", 0

    perf_values = iter([0.0, 0.0, 0.0, 1.3, 2.2, 2.2])
    monkeypatch.setattr(gull_main.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.exec_batch(
        gull_main.BatchExecRequest(
            commands=["open https://example.com", "snapshot -i", "get title"],
            timeout=2,
            stop_on_error=False,
        )
    )

    assert captured_timeouts == [2.0]
    assert response.total_steps == 3
    assert response.completed_steps == 1
    assert response.success is False
    assert len(response.results) == 1


@pytest.mark.asyncio
async def test_exec_batch_uses_remaining_budget_without_forced_minimum(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_timeouts: list[float] = []

    async def fake_run(_cmd: str, **kwargs):
        captured_timeouts.append(kwargs["timeout"])
        return "ok", "", 0

    perf_values = iter([0.0, 1.7, 1.7, 1.95, 2.0])
    monkeypatch.setattr(gull_main.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.exec_batch(
        gull_main.BatchExecRequest(
            commands=["snapshot -i"],
            timeout=2,
            stop_on_error=True,
        )
    )

    assert response.completed_steps == 1
    assert response.success is True
    assert len(captured_timeouts) == 1
    assert 0 < captured_timeouts[0] <= 0.3 + 1e-9
    assert captured_timeouts[0] < 1.0

@pytest.mark.asyncio
async def test_exec_batch_stop_on_error_true_stops_at_first_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    executed: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        executed.append(cmd)
        if cmd == "snapshot -i":
            return "", "step failed", 2
        return "ok", "", 0

    tick = {"value": 0.0}

    def fake_perf_counter() -> float:
        tick["value"] += 0.01
        return tick["value"]

    monkeypatch.setattr(gull_main.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.exec_batch(
        gull_main.BatchExecRequest(
            commands=["open https://example.com", "snapshot -i", "get title"],
            timeout=30,
            stop_on_error=True,
        )
    )

    assert executed == ["open https://example.com", "snapshot -i"]
    assert response.total_steps == 3
    assert response.completed_steps == 2
    assert response.success is False
    assert response.results[-1].cmd == "snapshot -i"
    assert response.results[-1].exit_code == 2


@pytest.mark.asyncio
async def test_exec_batch_stop_on_error_false_continues_but_stays_unsuccessful(
    monkeypatch: pytest.MonkeyPatch,
):
    executed: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        executed.append(cmd)
        if cmd == "snapshot -i":
            return "", "step failed", 1
        return "ok", "", 0

    tick = {"value": 0.0}

    def fake_perf_counter() -> float:
        tick["value"] += 0.01
        return tick["value"]

    monkeypatch.setattr(gull_main.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.exec_batch(
        gull_main.BatchExecRequest(
            commands=["open https://example.com", "snapshot -i", "get title"],
            timeout=30,
            stop_on_error=False,
        )
    )

    assert executed == ["open https://example.com", "snapshot -i", "get title"]
    assert response.total_steps == 3
    assert response.completed_steps == 3
    assert response.success is False
    assert any(step.exit_code != 0 for step in response.results)


@pytest.mark.asyncio
async def test_health_unhealthy_when_agent_browser_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gull_main.shutil, "which", lambda _name: None)

    response = await gull_main.health()

    assert response.status == "unhealthy"
    assert response.browser_active is False


@pytest.mark.asyncio
async def test_health_healthy_when_probe_succeeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
    )

    async def fake_run(_cmd: str, **_kwargs):
        return gull_main.SESSION_NAME, "", 0

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.health()

    assert response.status == "healthy"
    assert response.browser_active is True


@pytest.mark.asyncio
async def test_health_healthy_when_probe_succeeds_without_active_session(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser")

    async def fake_run(_cmd: str, **_kwargs):
        return "other-session", "", 0

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.health()

    assert response.status == "healthy"
    assert response.browser_active is False


@pytest.mark.asyncio
async def test_health_degraded_when_probe_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
    )

    async def fake_run(_cmd: str, **_kwargs):
        return "", "probe failed", 2

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.health()

    assert response.status == "degraded"
    assert response.browser_active is False
