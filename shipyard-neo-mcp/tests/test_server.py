"""Unit tests for MCP server tool handlers."""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest
from shipyard_neo_mcp import server as mcp_server

from shipyard_neo import BayError
from shipyard_neo.types import SkillCandidateStatus, SkillReleaseStage


class FakePythonCapability:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def exec(
        self,
        code: str,
        *,
        timeout: int = 30,
        include_code: bool = False,
        description: str | None = None,
        tags: str | None = None,
    ):
        self.calls.append(
            {
                "code": code,
                "timeout": timeout,
                "include_code": include_code,
                "description": description,
                "tags": tags,
            }
        )
        return SimpleNamespace(
            success=True,
            output="ok\n",
            error=None,
            execution_id="exec-123",
            execution_time_ms=8,
            code=code,
        )


class FakeShellCapability:
    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int = 30,
        include_code: bool = False,
        description: str | None = None,
        tags: str | None = None,
    ):
        return SimpleNamespace(
            success=True,
            output="shell-output\n",
            error=None,
            exit_code=0,
            execution_id="exec-shell-1",
            execution_time_ms=5,
            command=command if include_code else None,
        )


class FakeBrowserCapability:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int = 30,
        description: str | None = None,
        tags: str | None = None,
        learn: bool = False,
        include_trace: bool = False,
    ):
        self.calls.append(
            {
                "cmd": cmd,
                "timeout": timeout,
                "description": description,
                "tags": tags,
                "learn": learn,
                "include_trace": include_trace,
            }
        )
        return SimpleNamespace(
            success=True,
            output="snapshot output\n",
            error=None,
            exit_code=0,
            execution_id="exec-browser-1",
            execution_time_ms=11,
            trace_ref="blob:trace-browser-1" if include_trace else None,
        )

    async def exec_batch(
        self,
        commands: list[str],
        *,
        timeout: int = 60,
        stop_on_error: bool = True,
        description: str | None = None,
        tags: str | None = None,
        learn: bool = False,
        include_trace: bool = False,
    ):
        self.calls.append(
            {
                "commands": commands,
                "timeout": timeout,
                "stop_on_error": stop_on_error,
                "description": description,
                "tags": tags,
                "learn": learn,
                "include_trace": include_trace,
            }
        )
        results = [
            SimpleNamespace(
                cmd=cmd,
                stdout=f"ok-{i}\n",
                stderr="",
                exit_code=0,
                step_index=i,
                duration_ms=10 + i,
            )
            for i, cmd in enumerate(commands)
        ]
        return SimpleNamespace(
            results=results,
            total_steps=len(commands),
            completed_steps=len(commands),
            success=True,
            duration_ms=sum(r.duration_ms for r in results),
            execution_id="exec-browser-batch-1",
            execution_time_ms=31,
            trace_ref="blob:trace-browser-batch-1" if include_trace else None,
        )


class FakeFilesystem:
    async def read_file(self, _path: str) -> str:
        return "content"

    async def write_file(self, _path: str, _content: str) -> None:
        return None

    async def list_dir(self, _path: str):
        return []

    async def delete(self, _path: str) -> None:
        return None


class FakeSandbox:
    def __init__(self) -> None:
        self.python = FakePythonCapability()
        self.shell = FakeShellCapability()
        self.filesystem = FakeFilesystem()
        self.browser = FakeBrowserCapability()

    async def get_execution_history(
        self,
        *,
        exec_type: str | None = None,
        success_only: bool = False,
        limit: int = 50,
        tags: str | None = None,
        has_notes: bool = False,
        has_description: bool = False,
    ):
        _ = (exec_type, success_only, limit, tags, has_notes, has_description)
        return SimpleNamespace(
            total=1,
            entries=[
                SimpleNamespace(
                    id="exec-1",
                    exec_type="python",
                    success=True,
                    execution_time_ms=6,
                    description="desc",
                    tags="tag1,tag2",
                )
            ],
        )

    async def get_execution(self, execution_id: str):
        return SimpleNamespace(
            id=execution_id,
            exec_type="python",
            success=True,
            execution_time_ms=3,
            tags="tag1",
            description="desc",
            notes="note",
            code="print('x')",
            output="x\n",
            error=None,
        )

    async def get_last_execution(self, *, exec_type: str | None = None):
        _ = exec_type
        return SimpleNamespace(
            id="exec-last",
            exec_type="shell",
            success=True,
            execution_time_ms=9,
            code="echo hi",
        )

    async def annotate_execution(
        self,
        execution_id: str,
        *,
        description: str | None = None,
        tags: str | None = None,
        notes: str | None = None,
    ):
        return SimpleNamespace(
            id=execution_id,
            description=description,
            tags=tags,
            notes=notes,
        )

    async def delete(self) -> None:
        return None


class FakeSkills:
    def __init__(self) -> None:
        self.last_promote_stage: str | None = None

    async def create_payload(
        self,
        *,
        payload: dict | list,
        kind: str = "generic",
    ):
        _ = payload
        return SimpleNamespace(payload_ref="blob:blob-1", kind=kind)

    async def get_payload(self, payload_ref: str):
        return SimpleNamespace(
            payload_ref=payload_ref,
            kind="candidate_payload",
            payload={"commands": ["open about:blank"]},
        )

    async def create_candidate(
        self,
        *,
        skill_key: str,
        source_execution_ids: list[str],
        scenario_key: str | None = None,
        payload_ref: str | None = None,
    ):
        _ = (scenario_key, payload_ref)
        return SimpleNamespace(
            id="sc-1",
            skill_key=skill_key,
            status=SkillCandidateStatus.DRAFT,
            source_execution_ids=source_execution_ids,
        )

    async def evaluate_candidate(
        self,
        candidate_id: str,
        *,
        passed: bool,
        score: float | None = None,
        benchmark_id: str | None = None,
        report: str | None = None,
    ):
        _ = (benchmark_id, report)
        return SimpleNamespace(
            id="se-1",
            candidate_id=candidate_id,
            passed=passed,
            score=score,
        )

    async def promote_candidate(self, candidate_id: str, *, stage: str = "canary"):
        self.last_promote_stage = stage
        return SimpleNamespace(
            id="sr-1",
            skill_key="csv-loader",
            candidate_id=candidate_id,
            version=1,
            stage=SkillReleaseStage.CANARY,
            is_active=True,
        )

    async def list_candidates(
        self,
        *,
        status: str | None = None,
        skill_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _ = (status, skill_key, limit, offset)
        return SimpleNamespace(total=0, items=[])

    async def list_releases(
        self,
        *,
        skill_key: str | None = None,
        active_only: bool = False,
        stage: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _ = (skill_key, active_only, stage, limit, offset)
        return SimpleNamespace(total=0, items=[])

    async def rollback_release(self, release_id: str):
        return SimpleNamespace(
            id="sr-2",
            skill_key="csv-loader",
            candidate_id="sc-1",
            version=2,
            stage=SkillReleaseStage.STABLE,
            is_active=True,
            rollback_of=release_id,
        )


class FakeClient:
    def __init__(self, skills: FakeSkills | None = None) -> None:
        self.skills = skills or FakeSkills()
        self.created_sandbox_ids: list[str] = []

    async def create_sandbox(self, profile: str, ttl: int):
        sandbox = SimpleNamespace(
            id="sbx-new",
            profile=profile,
            status=SimpleNamespace(value="ready"),
            capabilities=["python", "shell", "filesystem"],
            ttl=ttl,
        )
        self.created_sandbox_ids.append(sandbox.id)
        return sandbox

    async def get_sandbox(self, sandbox_id: str):
        return mcp_server._sandboxes[sandbox_id]

    async def list_profiles(self, **kwargs):
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    id="python-default",
                    description="Default Python sandbox",
                    capabilities=["python", "shell", "filesystem"],
                    idle_timeout=300,
                    containers=None,
                ),
                SimpleNamespace(
                    id="browser-default",
                    description="Browser-enabled sandbox",
                    capabilities=["python", "shell", "filesystem", "browser"],
                    idle_timeout=600,
                    containers=None,
                ),
            ]
        )


@pytest.fixture(autouse=True)
def reset_globals(monkeypatch):
    """Isolate global state between tests."""
    monkeypatch.setattr(mcp_server, "_client", None)
    monkeypatch.setattr(mcp_server, "_sandboxes", OrderedDict())


@pytest.mark.asyncio
async def test_list_tools_contains_history_and_skill_tools():
    tools = await mcp_server.list_tools()
    names = {tool.name for tool in tools}

    assert "get_execution_history" in names
    assert "annotate_execution" in names
    assert "create_skill_payload" in names
    assert "get_skill_payload" in names
    assert "create_skill_candidate" in names
    assert "promote_skill_candidate" in names
    assert "rollback_skill_release" in names
    assert "execute_browser" in names
    assert "execute_browser_batch" in names
    assert "list_profiles" in names


@pytest.mark.asyncio
async def test_call_tool_requires_initialized_client():
    response = await mcp_server.call_tool("unknown", {})
    assert len(response) == 1
    assert "BayClient not initialized" in response[0].text


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_returns_error_message():
    mcp_server._client = FakeClient()
    response = await mcp_server.call_tool("not_a_tool", {})
    assert len(response) == 1
    assert "Unknown tool: not_a_tool" in response[0].text


@pytest.mark.asyncio
async def test_execute_python_formats_success_with_metadata():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {
            "sandbox_id": "sbx-1",
            "code": "print('ok')",
            "include_code": True,
            "description": "desc",
            "tags": "tag1",
        },
    )

    assert len(response) == 1
    text = response[0].text
    assert "Execution successful" in text
    assert "execution_id: exec-123" in text
    assert "execution_time_ms: 8" in text
    assert "code:\nprint('ok')" in text
    assert fake_sandbox.python.calls[0]["description"] == "desc"
    assert fake_sandbox.python.calls[0]["tags"] == "tag1"


@pytest.mark.asyncio
async def test_get_execution_history_formats_entries():
    mcp_server._sandboxes["sbx-1"] = FakeSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "get_execution_history",
        {"sandbox_id": "sbx-1", "limit": 10, "success_only": True},
    )
    text = response[0].text
    assert "Total: 1" in text
    assert "- exec-1 | python | success=True | 6ms" in text
    assert "description: desc" in text
    assert "tags: tag1,tag2" in text


@pytest.mark.asyncio
async def test_get_execution_history_empty_message():
    class EmptyHistorySandbox(FakeSandbox):
        async def get_execution_history(self, **_kwargs):
            return SimpleNamespace(total=0, entries=[])

    mcp_server._sandboxes["sbx-1"] = EmptyHistorySandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "get_execution_history", {"sandbox_id": "sbx-1"}
    )
    assert response[0].text == "No execution history found."


@pytest.mark.asyncio
async def test_create_skill_candidate_tool_calls_sdk_manager():
    skills = FakeSkills()
    mcp_server._client = FakeClient(skills=skills)

    response = await mcp_server.call_tool(
        "create_skill_candidate",
        {"skill_key": "csv-loader", "source_execution_ids": ["exec-1", "exec-2"]},
    )
    text = response[0].text
    assert "Created skill candidate sc-1" in text
    assert "status: draft" in text
    assert "source_execution_ids: exec-1, exec-2" in text


@pytest.mark.asyncio
async def test_create_skill_payload_tool_calls_sdk_manager():
    skills = FakeSkills()
    mcp_server._client = FakeClient(skills=skills)

    response = await mcp_server.call_tool(
        "create_skill_payload",
        {"payload": {"commands": ["open about:blank"]}, "kind": "candidate_payload"},
    )
    text = response[0].text
    assert "Created skill payload blob:blob-1" in text
    assert "kind: candidate_payload" in text


@pytest.mark.asyncio
async def test_get_skill_payload_tool_formats_payload():
    skills = FakeSkills()
    mcp_server._client = FakeClient(skills=skills)

    response = await mcp_server.call_tool(
        "get_skill_payload",
        {"payload_ref": "blob:blob-1"},
    )
    text = response[0].text
    assert "payload_ref: blob:blob-1" in text
    assert "kind: candidate_payload" in text
    assert '"commands": ["open about:blank"]' in text


@pytest.mark.asyncio
async def test_promote_skill_candidate_defaults_to_canary():
    skills = FakeSkills()
    mcp_server._client = FakeClient(skills=skills)

    response = await mcp_server.call_tool(
        "promote_skill_candidate",
        {"candidate_id": "sc-1"},
    )
    text = response[0].text
    assert "Candidate promoted: sc-1" in text
    assert "stage: canary" in text
    assert skills.last_promote_stage == "canary"


@pytest.mark.asyncio
async def test_rollback_skill_release_formats_result():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "rollback_skill_release",
        {"release_id": "sr-1"},
    )
    text = response[0].text
    assert "Rollback completed." in text
    assert "rollback_of: sr-1" in text


@pytest.mark.asyncio
async def test_call_tool_surfaces_bay_errors():
    class ErrorSkills(FakeSkills):
        async def create_candidate(self, **_kwargs):
            raise BayError("upstream failure")

    mcp_server._client = FakeClient(skills=ErrorSkills())

    response = await mcp_server.call_tool(
        "create_skill_candidate",
        {"skill_key": "csv-loader", "source_execution_ids": ["exec-1"]},
    )
    assert "[internal_error] upstream failure" in response[0].text


@pytest.mark.asyncio
async def test_validation_error_for_missing_required_argument():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "sbx-1"},
    )
    assert response[0].text == "**Validation Error:** missing required field: code"


@pytest.mark.asyncio
async def test_validation_error_for_invalid_limit():
    mcp_server._client = FakeClient()
    mcp_server._sandboxes["sbx-1"] = FakeSandbox()

    response = await mcp_server.call_tool(
        "get_execution_history",
        {"sandbox_id": "sbx-1", "limit": -1},
    )
    assert "field 'limit' must be >= 1" in response[0].text


@pytest.mark.asyncio
async def test_execute_python_truncates_large_output():
    class LargeOutputPythonCapability:
        async def exec(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=True,
                output="x" * 13050,
                error=None,
                execution_id="exec-long",
                execution_time_ms=2,
                code="print('x')",
            )

    class LargeOutputSandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.python = LargeOutputPythonCapability()

    mcp_server._client = FakeClient()
    mcp_server._sandboxes["sbx-1"] = LargeOutputSandbox()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "sbx-1", "code": "print('x')"},
    )

    assert "truncated" in response[0].text
    assert "execution_id: exec-long" in response[0].text


def test_cache_eviction_keeps_bounded_size(monkeypatch):
    monkeypatch.setattr(mcp_server, "_MAX_SANDBOX_CACHE_SIZE", 2)
    mcp_server._sandboxes = OrderedDict()

    mcp_server._cache_sandbox(SimpleNamespace(id="sbx-1"))
    mcp_server._cache_sandbox(SimpleNamespace(id="sbx-2"))
    mcp_server._cache_sandbox(SimpleNamespace(id="sbx-3"))

    assert list(mcp_server._sandboxes.keys()) == ["sbx-2", "sbx-3"]


# -- Browser capability tests --


@pytest.mark.asyncio
async def test_execute_browser_formats_success():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {"sandbox_id": "sbx-1", "cmd": "open https://example.com"},
    )

    assert len(response) == 1
    text = response[0].text
    assert "Browser command successful" in text
    assert "exit code: 0" in text
    assert "snapshot output" in text
    assert "execution_id: exec-browser-1" in text
    assert "execution_time_ms: 11" in text
    assert fake_sandbox.browser.calls[0]["cmd"] == "open https://example.com"


@pytest.mark.asyncio
async def test_execute_browser_formats_failure():
    class FailBrowserCapability(FakeBrowserCapability):
        async def exec(
            self,
            cmd: str,
            *,
            timeout: int = 30,
            description: str | None = None,
            tags: str | None = None,
            learn: bool = False,
            include_trace: bool = False,
        ):
            _ = (description, tags, learn, include_trace)
            return SimpleNamespace(
                success=False,
                output="",
                error="element not found",
                exit_code=1,
            )

    class FailBrowserSandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.browser = FailBrowserCapability()

    mcp_server._sandboxes["sbx-1"] = FailBrowserSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {"sandbox_id": "sbx-1", "cmd": "click @e99"},
    )

    text = response[0].text
    assert "Browser command failed" in text
    assert "exit code: 1" in text
    assert "element not found" in text


@pytest.mark.asyncio
async def test_execute_browser_missing_cmd():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {"sandbox_id": "sbx-1"},
    )
    assert "missing required field: cmd" in response[0].text


@pytest.mark.asyncio
async def test_execute_browser_custom_timeout():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    await mcp_server.call_tool(
        "execute_browser",
        {
            "sandbox_id": "sbx-1",
            "cmd": "screenshot /workspace/page.png",
            "timeout": 120,
        },
    )

    assert fake_sandbox.browser.calls[0]["timeout"] == 120


@pytest.mark.asyncio
async def test_execute_browser_passes_learning_flags():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {
            "sandbox_id": "sbx-1",
            "cmd": "snapshot -i",
            "description": "capture controls",
            "tags": "browser,trace",
            "learn": True,
            "include_trace": True,
        },
    )

    call = fake_sandbox.browser.calls[0]
    assert call["description"] == "capture controls"
    assert call["tags"] == "browser,trace"
    assert call["learn"] is True
    assert call["include_trace"] is True
    assert "trace_ref: blob:trace-browser-1" in response[0].text


@pytest.mark.asyncio
async def test_execute_browser_without_trace_does_not_render_trace_ref():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {"sandbox_id": "sbx-1", "cmd": "snapshot -i", "include_trace": False},
    )

    text = response[0].text
    assert "execution_id: exec-browser-1" in text
    assert "trace_ref:" not in text


# -- Browser batch tests --


@pytest.mark.asyncio
async def test_execute_browser_batch_formats_success():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    commands = ["open https://example.com", "wait --load networkidle", "snapshot -i"]
    response = await mcp_server.call_tool(
        "execute_browser_batch",
        {"sandbox_id": "sbx-1", "commands": commands},
    )

    assert len(response) == 1
    text = response[0].text
    assert "Batch execution completed" in text
    assert "3/3 steps" in text
    assert "execution_id: exec-browser-batch-1" in text
    assert "execution_time_ms: 31" in text
    assert "✅ Step 0" in text
    assert "✅ Step 1" in text
    assert "✅ Step 2" in text
    assert "`open https://example.com`" in text
    assert "`snapshot -i`" in text


@pytest.mark.asyncio
async def test_execute_browser_batch_with_failure():
    class PartialFailBrowserCapability(FakeBrowserCapability):
        async def exec_batch(
            self,
            commands,
            *,
            timeout=60,
            stop_on_error=True,
            description: str | None = None,
            tags: str | None = None,
            learn: bool = False,
            include_trace: bool = False,
        ):
            _ = (description, tags, learn, include_trace)
            return SimpleNamespace(
                results=[
                    SimpleNamespace(
                        cmd="open https://example.com",
                        stdout="ok\n",
                        stderr="",
                        exit_code=0,
                        step_index=0,
                        duration_ms=10,
                    ),
                    SimpleNamespace(
                        cmd="click @e99",
                        stdout="",
                        stderr="element not found",
                        exit_code=1,
                        step_index=1,
                        duration_ms=5,
                    ),
                ],
                total_steps=3,
                completed_steps=2,
                success=False,
                duration_ms=15,
            )

    class PartialFailSandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.browser = PartialFailBrowserCapability()

    mcp_server._sandboxes["sbx-1"] = PartialFailSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser_batch",
        {
            "sandbox_id": "sbx-1",
            "commands": ["open https://example.com", "click @e99", "snapshot -i"],
        },
    )

    text = response[0].text
    assert "Batch execution failed" in text
    assert "2/3 steps" in text
    assert "✅ Step 0" in text
    assert "❌ Step 1" in text
    assert "element not found" in text


@pytest.mark.asyncio
async def test_execute_browser_batch_passes_stop_on_error():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    await mcp_server.call_tool(
        "execute_browser_batch",
        {
            "sandbox_id": "sbx-1",
            "commands": ["open https://example.com"],
            "stop_on_error": False,
            "timeout": 120,
        },
    )

    call = fake_sandbox.browser.calls[0]
    assert call["stop_on_error"] is False
    assert call["timeout"] == 120


@pytest.mark.asyncio
async def test_execute_browser_batch_passes_learning_flags():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser_batch",
        {
            "sandbox_id": "sbx-1",
            "commands": ["open https://example.com", "snapshot -i"],
            "description": "batch run",
            "tags": "browser,batch",
            "learn": True,
            "include_trace": True,
        },
    )

    call = fake_sandbox.browser.calls[0]
    assert call["description"] == "batch run"
    assert call["tags"] == "browser,batch"
    assert call["learn"] is True
    assert call["include_trace"] is True
    assert "trace_ref: blob:trace-browser-batch-1" in response[0].text


@pytest.mark.asyncio
async def test_execute_browser_batch_without_trace_does_not_render_trace_ref():
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-1"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser_batch",
        {
            "sandbox_id": "sbx-1",
            "commands": ["open https://example.com", "snapshot -i"],
            "include_trace": False,
        },
    )

    text = response[0].text
    assert "execution_id: exec-browser-batch-1" in text
    assert "trace_ref:" not in text


@pytest.mark.asyncio
async def test_execute_browser_handles_missing_optional_metadata_fields():
    class MinimalBrowserCapability(FakeBrowserCapability):
        async def exec(
            self,
            cmd: str,
            *,
            timeout: int = 30,
            description: str | None = None,
            tags: str | None = None,
            learn: bool = False,
            include_trace: bool = False,
        ):
            _ = (cmd, timeout, description, tags, learn, include_trace)
            return SimpleNamespace(
                success=True,
                output="ok\n",
                error=None,
                exit_code=0,
            )

    class MinimalBrowserSandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.browser = MinimalBrowserCapability()

    mcp_server._sandboxes["sbx-1"] = MinimalBrowserSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {"sandbox_id": "sbx-1", "cmd": "open about:blank"},
    )
    text = response[0].text
    assert "Browser command successful" in text
    assert "execution_id:" not in text
    assert "execution_time_ms:" not in text


@pytest.mark.asyncio
async def test_execute_browser_batch_empty_commands_is_validation_error():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser_batch",
        {"sandbox_id": "sbx-1", "commands": []},
    )
    assert "non-empty array" in response[0].text


@pytest.mark.asyncio
async def test_execute_browser_batch_rejects_non_string_command_items():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser_batch",
        {"sandbox_id": "sbx-1", "commands": ["open https://example.com", 123]},
    )
    assert "non-empty array of strings" in response[0].text


@pytest.mark.asyncio
async def test_execute_browser_rejects_non_boolean_include_trace():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_browser",
        {"sandbox_id": "sbx-1", "cmd": "snapshot -i", "include_trace": "yes"},
    )
    assert "field 'include_trace' must be a boolean" in response[0].text


# -- list_profiles tests --


@pytest.mark.asyncio
async def test_list_profiles_formats_output():
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool("list_profiles", {})

    assert len(response) == 1
    text = response[0].text
    assert "Available Profiles" in text
    assert "python-default" in text
    assert "browser-default" in text
    assert "browser" in text
    assert "idle_timeout=" in text


@pytest.mark.asyncio
async def test_list_profiles_empty():
    class EmptyProfileClient(FakeClient):
        async def list_profiles(self, **kwargs):
            return SimpleNamespace(items=[])

    mcp_server._client = EmptyProfileClient()

    response = await mcp_server.call_tool("list_profiles", {})
    assert response[0].text == "No profiles available."


# -- Guardrail tests --


@pytest.mark.asyncio
async def test_sandbox_id_format_validation_rejects_invalid():
    """sandbox_id with special characters should be rejected."""
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "../../../etc/passwd", "code": "print('x')"},
    )
    assert "invalid sandbox_id format" in response[0].text


@pytest.mark.asyncio
async def test_sandbox_id_format_validation_rejects_empty():
    """Empty sandbox_id should be rejected."""
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "", "code": "print('x')"},
    )
    assert "missing required field" in response[0].text


@pytest.mark.asyncio
async def test_sandbox_id_format_validation_rejects_too_long():
    """sandbox_id longer than 128 chars should be rejected."""
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "a" * 129, "code": "print('x')"},
    )
    assert "invalid sandbox_id format" in response[0].text


@pytest.mark.asyncio
async def test_sandbox_id_format_validation_accepts_valid():
    """Valid sandbox_id patterns should pass validation."""
    fake_sandbox = FakeSandbox()
    mcp_server._sandboxes["sbx-123_test"] = fake_sandbox
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "sbx-123_test", "code": "print('ok')"},
    )
    assert "Execution successful" in response[0].text


@pytest.mark.asyncio
async def test_write_file_rejects_oversized_content(monkeypatch):
    """write_file should reject content exceeding SHIPYARD_MAX_WRITE_FILE_BYTES."""
    monkeypatch.setattr(mcp_server, "_MAX_WRITE_FILE_BYTES", 100)
    mcp_server._sandboxes["sbx-1"] = FakeSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "write_file",
        {"sandbox_id": "sbx-1", "path": "test.txt", "content": "x" * 200},
    )
    assert "write_file content too large" in response[0].text
    assert "exceeds limit" in response[0].text


@pytest.mark.asyncio
async def test_write_file_accepts_content_within_limit(monkeypatch):
    """write_file should accept content within limit."""
    monkeypatch.setattr(mcp_server, "_MAX_WRITE_FILE_BYTES", 1000)
    mcp_server._sandboxes["sbx-1"] = FakeSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "write_file",
        {"sandbox_id": "sbx-1", "path": "test.txt", "content": "hello"},
    )
    assert "written successfully" in response[0].text


@pytest.mark.asyncio
async def test_timeout_error_returns_friendly_message():
    """TimeoutError from SDK calls should return a friendly message."""

    class TimeoutSandbox(FakeSandbox):
        class TimeoutPython:
            async def exec(self, *_args, **_kwargs):
                raise TimeoutError("timed out")

        def __init__(self):
            super().__init__()
            self.python = self.TimeoutPython()

    mcp_server._sandboxes["sbx-1"] = TimeoutSandbox()
    mcp_server._client = FakeClient()

    response = await mcp_server.call_tool(
        "execute_python",
        {"sandbox_id": "sbx-1", "code": "print('x')"},
    )
    assert "Timeout Error" in response[0].text


def test_validate_sandbox_id_accepts_hyphens_and_underscores():
    """_validate_sandbox_id should accept alphanumeric, hyphens, underscores."""
    result = mcp_server._validate_sandbox_id({"sandbox_id": "sbx-123_test"})
    assert result == "sbx-123_test"


def test_validate_sandbox_id_rejects_dots():
    """_validate_sandbox_id should reject dots."""
    with pytest.raises(ValueError, match="invalid sandbox_id format"):
        mcp_server._validate_sandbox_id({"sandbox_id": "sbx.123"})


def test_validate_sandbox_id_rejects_spaces():
    """_validate_sandbox_id should reject spaces."""
    with pytest.raises(ValueError, match="invalid sandbox_id format"):
        mcp_server._validate_sandbox_id({"sandbox_id": "sbx 123"})


def test_cache_eviction_logs_evicted_id(monkeypatch, caplog):
    """Cache eviction should log the evicted sandbox ID."""
    import logging

    monkeypatch.setattr(mcp_server, "_MAX_SANDBOX_CACHE_SIZE", 1)
    mcp_server._sandboxes = OrderedDict()

    with caplog.at_level(logging.DEBUG, logger="shipyard_neo_mcp"):
        mcp_server._cache_sandbox(SimpleNamespace(id="sbx-1"))
        mcp_server._cache_sandbox(SimpleNamespace(id="sbx-2"))

    assert "cache_evict" in caplog.text
    assert "sbx-1" in caplog.text
    assert list(mcp_server._sandboxes.keys()) == ["sbx-2"]


@pytest.mark.asyncio
async def test_create_sandbox_logs_info(caplog, monkeypatch):
    """create_sandbox should log sandbox creation."""
    import logging

    monkeypatch.setenv("SHIPYARD_ENDPOINT_URL", "http://localhost:8000")
    monkeypatch.setenv("SHIPYARD_ACCESS_TOKEN", "test-token")
    mcp_server._client = FakeClient()

    with caplog.at_level(logging.INFO, logger="shipyard_neo_mcp"):
        response = await mcp_server.call_tool("create_sandbox", {})

    assert "sandbox_created" in caplog.text
    assert "sbx-new" in caplog.text
    assert "Sandbox created successfully" in response[0].text


@pytest.mark.asyncio
async def test_delete_sandbox_logs_info(caplog):
    """delete_sandbox should log sandbox deletion."""
    import logging

    mcp_server._sandboxes["sbx-1"] = FakeSandbox()
    mcp_server._client = FakeClient()

    with caplog.at_level(logging.INFO, logger="shipyard_neo_mcp"):
        response = await mcp_server.call_tool("delete_sandbox", {"sandbox_id": "sbx-1"})

    assert "sandbox_deleted" in caplog.text
    assert "sbx-1" in caplog.text
    assert "deleted successfully" in response[0].text
