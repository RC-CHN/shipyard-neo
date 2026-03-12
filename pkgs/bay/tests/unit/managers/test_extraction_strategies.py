"""Unit tests for extraction strategies."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import LlmExtractionConfig
from app.services.skills.extraction import (
    ExtractionContext,
    ExtractionResult,
    LlmAssistedExtractionStrategy,
    RuleBasedExtractionStrategy,
    compute_payload_hash,
)


def _context() -> ExtractionContext:
    return ExtractionContext(
        owner="default",
        execution_id="exec-1",
        sandbox_id="sandbox-1",
        code="batch",
        description="Checkout flow",
        tags="skill:browser-checkout,learn",
    )


@pytest.mark.asyncio
async def test_rule_strategy_matches_legacy_segmentation_and_scoring():
    strategy = RuleBasedExtractionStrategy()
    steps = [
        {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0},
        {"kind": "individual_action", "cmd": "click @e1", "exit_code": 0},
        {"kind": "individual_action", "cmd": "snapshot -i", "exit_code": 0},
        {"kind": "individual_action", "cmd": "type @e2 hello", "exit_code": 0},
    ]
    segments = strategy.extract_actionable_segments(steps=steps)
    assert len(segments) == 1
    assert [step["cmd"] for step in segments[0]] == [
        "open https://example.com",
        "click @e1",
    ]

    results = await strategy.extract(segments=segments, context=_context())
    assert len(results) == 1
    assert results[0].skill_key == "browser-checkout"

    metrics = strategy.score_segment(segment=results[0].steps)
    assert metrics == {
        "score": 0.91,
        "replay_success": 1.0,
        "samples": 20,
        "error_rate": 0.0,
        "p95_duration": 0,
    }


@pytest.mark.asyncio
async def test_rule_strategy_extracts_variable_for_type_action():
    strategy = RuleBasedExtractionStrategy(variable_extraction_enabled=True)
    segment = [
        {"kind": "individual_action", "cmd": "click @from", "exit_code": 0},
        {"kind": "individual_action", "cmd": 'type @from "San Francisco"', "exit_code": 0},
    ]
    results = await strategy.extract(segments=[segment], context=_context())
    variables = results[0].variables
    assert variables is not None
    assert len(variables) == 1
    spec = next(iter(variables.values()))
    assert spec.type == "string"
    assert spec.default_value == "San Francisco"
    assert spec.action_index == 1
    assert spec.arg_position == 1


@pytest.mark.asyncio
async def test_rule_strategy_variable_extraction_handles_none_and_multiple():
    strategy = RuleBasedExtractionStrategy(variable_extraction_enabled=True)
    without_type = [
        {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0},
        {"kind": "individual_action", "cmd": "click @btn", "exit_code": 0},
    ]
    multiple_type = [
        {"kind": "individual_action", "cmd": "type @from Edinburgh", "exit_code": 0},
        {"kind": "individual_action", "cmd": "click @swap", "exit_code": 0},
        {"kind": "individual_action", "cmd": "type @to Manchester", "exit_code": 0},
    ]
    results = await strategy.extract(segments=[without_type, multiple_type], context=_context())
    assert results[0].variables is None
    assert results[1].variables is not None
    assert len(results[1].variables or {}) == 2
    action_indexes = sorted(spec.action_index for spec in (results[1].variables or {}).values())
    assert action_indexes == [0, 2]


def test_extraction_result_computes_payload_hash_from_ordered_cmds():
    steps = [{"cmd": "open https://a.com"}, {"cmd": "click @e1"}]
    result = ExtractionResult(skill_key="browser-a", steps=steps)
    assert result.payload_hash == compute_payload_hash(steps=steps)


@pytest.mark.asyncio
async def test_llm_strategy_successfully_parses_semantic_results(monkeypatch: pytest.MonkeyPatch):
    captured_request: dict[str, object] = {}

    async def _fake_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self
        captured_request["url"] = url
        captured_request["headers"] = headers or {}
        captured_request["json"] = json or {}
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json_module.dumps(
                            {
                                "results": [
                                    {
                                        "skill_key": "semantic-flight-search",
                                        "description": "Semantic split",
                                        "steps": [
                                            {"cmd": "open https://flights.example"},
                                            {"cmd": "click @search"},
                                        ],
                                        "variables": {
                                            "from_city": {
                                                "type": "string",
                                                "default_value": "Edinburgh",
                                                "action_index": 0,
                                                "arg_position": 1,
                                            }
                                        },
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json=payload)

    json_module = json
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    strategy = LlmAssistedExtractionStrategy(
        config=LlmExtractionConfig(
            enabled=True,
            api_base="https://llm.test/v1",
            api_key="k",
            model="test-model",
            timeout_seconds=3,
            max_tokens=1000,
        ),
        fallback=RuleBasedExtractionStrategy(),
    )
    segment = [
        {"cmd": "open https://flights.example", "exit_code": 0, "kind": "individual_action"},
        {"cmd": "click @search", "exit_code": 0, "kind": "individual_action"},
    ]
    results = await strategy.extract(segments=[segment], context=_context())

    assert len(results) == 1
    assert results[0].skill_key == "semantic-flight-search"
    assert results[0].variables is not None
    assert "response_format" in (captured_request["json"] or {})
    assert (captured_request["json"] or {}).get("model") == "test-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect",
    [
        httpx.TimeoutException("timeout"),
        httpx.ConnectError("connect fail", request=httpx.Request("POST", "https://llm.test/v1")),
    ],
)
async def test_llm_strategy_falls_back_on_timeout_or_connection(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: Exception,
):
    async def _failing_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, url, headers, json
        raise side_effect

    monkeypatch.setattr(httpx.AsyncClient, "post", _failing_post)

    strategy = LlmAssistedExtractionStrategy(
        config=LlmExtractionConfig(enabled=True, api_base="https://llm.test/v1", api_key="k"),
        fallback=RuleBasedExtractionStrategy(),
    )
    segment = [
        {"cmd": "open https://example.com", "exit_code": 0, "kind": "individual_action"},
        {"cmd": "click @e1", "exit_code": 0, "kind": "individual_action"},
    ]
    results = await strategy.extract(segments=[segment], context=_context())
    assert len(results) == 1
    assert results[0].skill_key == "browser-checkout"


@pytest.mark.asyncio
async def test_llm_strategy_falls_back_on_parse_failure(monkeypatch: pytest.MonkeyPatch):
    async def _bad_json_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "not-json"}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _bad_json_post)

    strategy = LlmAssistedExtractionStrategy(
        config=LlmExtractionConfig(enabled=True, api_base="https://llm.test/v1", api_key="k"),
        fallback=RuleBasedExtractionStrategy(),
    )
    segment = [
        {"cmd": "open https://example.com", "exit_code": 0, "kind": "individual_action"},
        {"cmd": "click @e1", "exit_code": 0, "kind": "individual_action"},
    ]
    results = await strategy.extract(segments=[segment], context=_context())
    assert len(results) == 1
    assert results[0].skill_key == "browser-checkout"


@pytest.mark.asyncio
async def test_llm_strategy_maps_http_status_error_to_connection_reason(
    monkeypatch: pytest.MonkeyPatch,
):
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    response = httpx.Response(503, request=request, json={"error": "upstream unavailable"})

    async def _raise_status_error(
        self: LlmAssistedExtractionStrategy,
        *,
        segments: list[list[dict[str, object]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        del self, segments, context
        raise httpx.HTTPStatusError("upstream error", request=request, response=response)

    captured: dict[str, str] = {}

    async def _capture_reason(
        self: LlmAssistedExtractionStrategy,
        *,
        reason: str,
        segments: list[list[dict[str, object]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        captured["reason"] = reason
        return await RuleBasedExtractionStrategy().extract(segments=segments, context=context)

    monkeypatch.setattr(LlmAssistedExtractionStrategy, "_extract_via_llm", _raise_status_error)
    monkeypatch.setattr(LlmAssistedExtractionStrategy, "_fallback_with_reason", _capture_reason)

    strategy = LlmAssistedExtractionStrategy(
        config=LlmExtractionConfig(enabled=True, api_base="https://llm.test/v1", api_key="k"),
        fallback=RuleBasedExtractionStrategy(),
    )
    segment = [
        {"cmd": "open https://example.com", "exit_code": 0, "kind": "individual_action"},
        {"cmd": "click @e1", "exit_code": 0, "kind": "individual_action"},
    ]
    results = await strategy.extract(segments=[segment], context=_context())

    assert captured["reason"] == "connection_error"
    assert len(results) == 1
    assert results[0].skill_key == "browser-checkout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_effect", "expected_reason"),
    [
        (httpx.TimeoutException("timeout"), "timeout"),
        (
            httpx.ConnectError(
                "connect fail",
                request=httpx.Request("POST", "https://llm.test/v1/chat/completions"),
            ),
            "connection_error",
        ),
        (ValueError("bad parse"), "parse_error"),
    ],
)
async def test_llm_strategy_logs_structured_fallback_event(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: Exception,
    expected_reason: str,
):
    async def _raise(
        self: LlmAssistedExtractionStrategy,
        *,
        segments: list[list[dict[str, object]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        del self, segments, context
        raise side_effect

    events: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def warning(self, event: str, **kwargs: object) -> None:
            events.append((event, kwargs))

    monkeypatch.setattr(LlmAssistedExtractionStrategy, "_extract_via_llm", _raise)

    strategy = LlmAssistedExtractionStrategy(
        config=LlmExtractionConfig(enabled=True, api_base="https://llm.test/v1", api_key="k"),
        fallback=RuleBasedExtractionStrategy(),
    )
    strategy._log = _FakeLog()

    segment = [
        {"cmd": "open https://example.com", "exit_code": 0, "kind": "individual_action"},
        {"cmd": "click @e1", "exit_code": 0, "kind": "individual_action"},
    ]
    results = await strategy.extract(segments=[segment], context=_context())

    assert len(results) == 1
    assert events == [
        (
            "skills.browser.extraction.llm_fallback",
            {"reason": expected_reason, "execution_id": "exec-1"},
        )
    ]
