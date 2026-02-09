"""Unit tests for MCP server tool handlers."""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from shipyard_neo import BayError
from shipyard_neo.types import SkillCandidateStatus, SkillReleaseStage
from shipyard_neo_mcp import server as mcp_server


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
    assert "create_skill_candidate" in names
    assert "promote_skill_candidate" in names
    assert "rollback_skill_release" in names


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

    response = await mcp_server.call_tool("get_execution_history", {"sandbox_id": "sbx-1"})
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
