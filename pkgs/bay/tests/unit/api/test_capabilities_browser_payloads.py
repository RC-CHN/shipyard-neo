"""Unit tests for browser trace payload helpers in capabilities API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.capabilities import (
    _build_browser_batch_trace_payload,
    _build_browser_exec_trace_payload,
    get_browser_trace,
)


def test_build_browser_exec_trace_payload_sets_single_step_fields():
    payload = _build_browser_exec_trace_payload(
        cmd="open about:blank",
        result_output="opened",
        result_error=None,
        exit_code=0,
    )
    assert payload["kind"] == "browser_exec_trace"
    assert len(payload["steps"]) == 1
    step = payload["steps"][0]
    assert step["kind"] == "individual_action"
    assert step["cmd"] == "open about:blank"
    assert step["stdout"] == "opened"
    assert step["stderr"] == ""
    assert step["exit_code"] == 0


def test_build_browser_batch_trace_payload_skips_non_dict_steps_and_coerces_exit_code():
    payload = _build_browser_batch_trace_payload(
        request_commands=["open about:blank", "snapshot -i", "click @e1"],
        raw_result={
            "results": [
                {"cmd": "open about:blank", "stdout": "ok", "stderr": "", "exit_code": "0"},
                "not-a-step",
                {"stdout": "snapshot", "stderr": "", "exit_code": None, "step_index": 2},
            ],
            "total_steps": 3,
            "completed_steps": 2,
            "success": False,
            "duration_ms": 42,
        },
    )
    assert payload["kind"] == "browser_batch_trace"
    assert payload["total_steps"] == 3
    assert payload["completed_steps"] == 2
    assert payload["success"] is False
    assert payload["duration_ms"] == 42
    assert len(payload["steps"]) == 2
    assert payload["steps"][0]["exit_code"] == 0
    assert payload["steps"][1]["cmd"] == "click @e1"
    assert payload["steps"][1]["exit_code"] == -1


def test_build_browser_batch_trace_payload_uses_request_length_defaults():
    payload = _build_browser_batch_trace_payload(
        request_commands=["open about:blank"],
        raw_result={"results": []},
    )
    assert payload["total_steps"] == 1
    assert payload["completed_steps"] == 0
    assert payload["success"] is False
    assert payload["duration_ms"] == 0
    assert payload["steps"] == []


class _FakeSkillService:
    async def get_payload_with_blob_by_ref(self, *, owner: str, payload_ref: str):
        _ = owner
        return (
            SimpleNamespace(id="blob-1", kind="browser_trace"),
            {"kind": "browser_exec_trace", "steps": [{"cmd": "open about:blank"}]},
        )


@pytest.mark.asyncio
async def test_get_browser_trace_keeps_response_shape_while_using_generic_payload_lookup():
    response = await get_browser_trace(
        sandbox_id="sbx-1",
        trace_ref="blob:blob-1",
        sandbox=SimpleNamespace(id="sbx-1"),
        skill_svc=_FakeSkillService(),
        owner="default",
    )

    assert response.trace_ref == "blob:blob-1"
    assert response.trace["kind"] == "browser_exec_trace"
    assert response.trace["steps"][0]["cmd"] == "open about:blank"
