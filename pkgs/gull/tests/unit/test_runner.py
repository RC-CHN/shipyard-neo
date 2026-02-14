"""Unit tests for Gull command runner.

Focuses on the internal runner helpers and API functions:
- `_run_agent_browser()` argv construction, quoting, timeouts, and error paths
- `_ensure_browser_ready()` probe/prewarm behavior and concurrency lock
- `exec_command()` and `exec_batch()` policy around profile injection

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
    returncode: int | None = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout_bytes, self.stderr_bytes

    def kill(self) -> None:
        # emulate subprocess API
        self.returncode = -9

    async def wait(self) -> int | None:
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
async def test_run_agent_browser_returns_minus_one_when_agent_browser_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_create_subprocess_exec(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(
        gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    stdout, stderr, code = await gull_main._run_agent_browser(
        "open https://example.com",
        session="s",
        profile="/p",
        timeout=1,
    )

    assert stdout == ""
    assert "agent-browser not found" in stderr
    assert code == -1


@pytest.mark.asyncio
async def test_run_agent_browser_returns_minus_one_on_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_create_subprocess_exec(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    stdout, stderr, code = await gull_main._run_agent_browser(
        "open https://example.com",
        session="s",
        profile="/p",
        timeout=1,
    )

    assert stdout == ""
    assert "Failed to execute command" in stderr
    assert "boom" in stderr
    assert code == -1


@pytest.mark.asyncio
async def test_run_agent_browser_returncode_none_coerces_to_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProcess(b"out", b"", None)

    monkeypatch.setattr(
        gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    stdout, stderr, code = await gull_main._run_agent_browser(
        "open about:blank",
        session="s",
        profile="/p",
        timeout=1,
    )

    assert stdout == "out"
    assert stderr == ""
    assert code == 0


@pytest.mark.asyncio
async def test_ensure_browser_ready_probe_success_skips_open(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        calls.append(cmd)
        if cmd == "session list":
            return "", "", 0
        return "", "", 0

    # Reset state
    monkeypatch.setattr(gull_main, "_browser_ready", False)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    await gull_main._ensure_browser_ready()

    assert gull_main._browser_ready is True
    assert calls == ["session list"]


@pytest.mark.asyncio
async def test_ensure_browser_ready_probe_fail_then_open_success_sets_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        calls.append(cmd)
        if cmd == "session list":
            return "", "probe failed", 2
        if cmd == "open about:blank":
            return "ok", "", 0
        return "", "", 0

    monkeypatch.setattr(gull_main, "_browser_ready", False)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    await gull_main._ensure_browser_ready()

    assert gull_main._browser_ready is True
    assert calls == ["session list", "open about:blank"]


@pytest.mark.asyncio
async def test_ensure_browser_ready_is_idempotent_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
):
    """Concurrent calls should not trigger multiple probes/prewarms."""
    calls: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        # Make the race window larger so we'd see duplicated calls if lock is broken.
        await asyncio.sleep(0.01)
        calls.append(cmd)
        # Probe succeeds.
        if cmd == "session list":
            return "", "", 0
        return "", "", 0

    monkeypatch.setattr(gull_main, "_browser_ready", False)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    await asyncio.gather(*[gull_main._ensure_browser_ready() for _ in range(20)])

    assert gull_main._browser_ready is True
    assert calls.count("session list") == 1
    assert calls.count("open about:blank") == 0


@pytest.mark.asyncio
async def test_ensure_browser_ready_probe_fail_and_open_fail_keeps_not_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        calls.append(cmd)
        if cmd == "session list":
            return "", "probe failed", 2
        if cmd == "open about:blank":
            return "", "browser failed", 1
        return "", "", 0

    monkeypatch.setattr(gull_main, "_browser_ready", False)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    await gull_main._ensure_browser_ready()

    assert gull_main._browser_ready is False
    assert calls == ["session list", "open about:blank"]


@pytest.mark.asyncio
async def test_exec_command_omits_profile_when_browser_ready_true(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_ensure_ready() -> None:
        return None

    async def fake_run(_cmd: str, **kwargs):
        captured.update(kwargs)
        return "", "", 0

    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)
    monkeypatch.setattr(gull_main, "_browser_ready", True)

    await gull_main.exec_command(gull_main.ExecRequest(cmd="open about:blank", timeout=5))

    assert captured["profile"] is None


@pytest.mark.asyncio
async def test_exec_command_injects_profile_when_browser_ready_false(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    async def fake_ensure_ready() -> None:
        return None

    async def fake_run(_cmd: str, **kwargs):
        captured.update(kwargs)
        return "", "", 0

    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)
    monkeypatch.setattr(gull_main, "_browser_ready", False)
    monkeypatch.setattr(gull_main, "BROWSER_PROFILE_DIR", "/workspace/.browser/profile")

    await gull_main.exec_command(gull_main.ExecRequest(cmd="open about:blank", timeout=5))

    assert captured["profile"] == "/workspace/.browser/profile"


@pytest.mark.asyncio
async def test_exec_batch_does_not_execute_step_when_budget_exactly_exhausted(
    monkeypatch: pytest.MonkeyPatch,
):
    executed: list[str] = []

    async def fake_run(cmd: str, **_kwargs):
        executed.append(cmd)
        return "ok", "", 0

    async def fake_ensure_ready() -> None:
        return None

    # perf_counter() call order in exec_batch():
    # 1) batch_start
    # 2) elapsed check (step 0)
    # 3) step_start (step 0)
    # 4) step_end (step 0)
    # 5) elapsed check (step 1) -> remaining becomes 0 -> break
    # 6) total_duration_ms at the end
    perf_values = iter([0.0, 0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(gull_main.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.exec_batch(
        gull_main.BatchExecRequest(commands=["open about:blank", "get title"], timeout=2)
    )

    assert executed == ["open about:blank"]
    assert response.total_steps == 2
    assert response.completed_steps == 1
    assert response.success is False


@pytest.mark.asyncio
async def test_exec_batch_stops_when_budget_exhausted_before_next_step(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_timeouts: list[float] = []

    async def fake_run(_cmd: str, **kwargs):
        captured_timeouts.append(kwargs["timeout"])
        return "ok", "", 0

    async def fake_ensure_ready() -> None:
        # exec_batch() now calls _ensure_browser_ready() once before executing steps.
        # Patch it out here so we can assert purely on per-step timeout budgeting.
        return None

    perf_values = iter([0.0, 0.0, 0.0, 1.3, 2.2, 2.2])
    monkeypatch.setattr(gull_main.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
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

    async def fake_ensure_ready() -> None:
        # exec_batch() now calls _ensure_browser_ready() once before executing steps.
        # Patch it out here so we can assert purely on per-step timeout budgeting.
        return None

    perf_values = iter([0.0, 1.7, 1.7, 1.95, 2.0])
    monkeypatch.setattr(gull_main.time, "perf_counter", lambda: next(perf_values))
    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
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

    async def fake_ensure_ready() -> None:
        # exec_batch() calls _ensure_browser_ready() once before executing steps.
        # Patch it out so this test asserts only stop-on-error behavior.
        return None

    tick = {"value": 0.0}

    def fake_perf_counter() -> float:
        tick["value"] += 0.01
        return tick["value"]

    monkeypatch.setattr(gull_main.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
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

    async def fake_ensure_ready() -> None:
        # exec_batch() calls _ensure_browser_ready() once before executing steps.
        # Patch it out so this test asserts only stop-on-error=False behavior.
        return None

    tick = {"value": 0.0}

    def fake_perf_counter() -> float:
        tick["value"] += 0.01
        return tick["value"]

    monkeypatch.setattr(gull_main.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(gull_main, "_ensure_browser_ready", fake_ensure_ready)
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
async def test_health_unhealthy_when_agent_browser_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(gull_main.shutil, "which", lambda _name: None)

    response = await gull_main.health()

    assert response.status == "unhealthy"
    assert response.browser_active is False
    assert response.browser_ready is False


@pytest.mark.asyncio
async def test_health_healthy_when_probe_succeeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
    )
    monkeypatch.setattr(gull_main, "_browser_ready", True)

    async def fake_run(_cmd: str, **_kwargs):
        return gull_main.SESSION_NAME, "", 0

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    response = await gull_main.health()

    assert response.status == "healthy"
    assert response.browser_active is True
    assert response.browser_ready is True


@pytest.mark.asyncio
async def test_health_healthy_when_probe_succeeds_without_active_session(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
    )

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


@pytest.mark.asyncio
async def test_health_browser_ready_reflects_prewarm_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """browser_ready field reflects _browser_ready module state."""
    monkeypatch.setattr(
        gull_main.shutil, "which", lambda _name: "/usr/bin/agent-browser"
    )

    async def fake_run(_cmd: str, **_kwargs):
        return gull_main.SESSION_NAME, "", 0

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    # Not warmed yet
    monkeypatch.setattr(gull_main, "_browser_ready", False)
    response = await gull_main.health()
    assert response.browser_ready is False

    # After warming
    monkeypatch.setattr(gull_main, "_browser_ready", True)
    response = await gull_main.health()
    assert response.browser_ready is True


# ---------------------------------------------------------------------------
# Lifespan pre-warming tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_prewarm_sets_browser_ready_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Successful pre-warm sets _browser_ready = True."""
    monkeypatch.setattr(gull_main, "BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(gull_main, "_browser_ready", False)

    async def fake_run(cmd: str, **_kwargs):
        # _ensure_browser_ready() probes first with `session list`.
        # Make the probe fail so lifespan then exercises the open/about:blank pre-warm path.
        if cmd == "session list":
            return "", "probe failed", 2
        if "open" in cmd:
            return "ok", "", 0
        return "", "", 0  # close command

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    async with gull_main.lifespan(gull_main.app):
        assert gull_main._browser_ready is True

    # After shutdown, should be False
    assert gull_main._browser_ready is False


@pytest.mark.asyncio
async def test_lifespan_prewarm_stays_false_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Pre-warm with non-zero exit code does not set _browser_ready."""
    monkeypatch.setattr(gull_main, "BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(gull_main, "_browser_ready", False)

    async def fake_run(cmd: str, **_kwargs):
        # Make probe fail so we actually attempt pre-warm open/about:blank.
        if cmd == "session list":
            return "", "probe failed", 2
        if "open" in cmd:
            return "", "browser failed to start", 1
        return "", "", 0  # close command

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    async with gull_main.lifespan(gull_main.app):
        assert gull_main._browser_ready is False


@pytest.mark.asyncio
async def test_lifespan_prewarm_stays_false_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Pre-warm exception does not prevent startup."""
    monkeypatch.setattr(gull_main, "BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(gull_main, "_browser_ready", False)

    async def fake_run(cmd: str, **_kwargs):
        # Make probe fail so we actually attempt pre-warm open/about:blank.
        if cmd == "session list":
            return "", "probe failed", 2
        if "open" in cmd:
            raise RuntimeError("agent-browser crashed")
        return "", "", 0  # close command

    monkeypatch.setattr(gull_main, "_run_agent_browser", fake_run)

    async with gull_main.lifespan(gull_main.app):
        # Service starts even though pre-warm failed
        assert gull_main._browser_ready is False
