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
async def test_run_agent_browser_injects_session_and_profile(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return _FakeProcess(b"out", b"err", 0)

    monkeypatch.setattr(gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

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

    monkeypatch.setattr(gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

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

    monkeypatch.setattr(gull_main.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    stdout, stderr, code = await gull_main._run_agent_browser(
        "snapshot -i",
        session="s",
        profile="/p",
        timeout=0.01,
    )

    assert stdout == ""
    assert "timed out" in stderr
    assert code == -1
