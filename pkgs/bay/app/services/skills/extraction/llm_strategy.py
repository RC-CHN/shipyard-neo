"""LLM-assisted extraction strategy with automatic fallback."""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from app.config import LlmExtractionConfig
from app.services.skills.extraction.base import (
    ExtractionContext,
    ExtractionResult,
    VariableSpec,
)
from app.services.skills.extraction.rule_strategy import RuleBasedExtractionStrategy


class LlmAssistedExtractionStrategy:
    """Semantic extraction backed by an OpenAI-compatible HTTP API."""

    def __init__(
        self,
        *,
        config: LlmExtractionConfig,
        fallback: RuleBasedExtractionStrategy,
    ) -> None:
        self._config = config
        self._fallback = fallback
        self._log = structlog.get_logger().bind(component="browser_learning_extraction_llm")

    async def extract(
        self,
        *,
        segments: list[list[dict[str, Any]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        if not segments:
            return []
        try:
            return await self._extract_via_llm(segments=segments, context=context)
        except httpx.TimeoutException:
            return await self._fallback_with_reason(
                reason="timeout",
                segments=segments,
                context=context,
            )
        except (httpx.RequestError, httpx.HTTPStatusError):
            return await self._fallback_with_reason(
                reason="connection_error",
                segments=segments,
                context=context,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return await self._fallback_with_reason(
                reason="parse_error",
                segments=segments,
                context=context,
            )

    async def _extract_via_llm(
        self,
        *,
        segments: list[list[dict[str, Any]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        response_payload = await self._call_llm(segments=segments, context=context)

        choices = response_payload["choices"]
        first = choices[0]
        message = first["message"]
        content = message["content"]

        if isinstance(content, list):
            content_text = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            )
        elif isinstance(content, str):
            content_text = content
        else:
            raise ValueError("Unsupported LLM response content type")

        parsed = json.loads(content_text)
        results_raw = parsed["results"]
        if not isinstance(results_raw, list):
            raise ValueError("Invalid extraction response: results must be a list")

        extracted: list[ExtractionResult] = []
        for item in results_raw:
            if not isinstance(item, dict):
                raise ValueError("Invalid extraction response: result item must be object")

            skill_key = str(item["skill_key"]).strip()
            if not skill_key:
                raise ValueError("Invalid extraction response: skill_key cannot be empty")

            steps_raw = item["steps"]
            if not isinstance(steps_raw, list):
                raise ValueError("Invalid extraction response: steps must be a list")
            steps = [step for step in steps_raw if isinstance(step, dict)]

            desc_raw = item.get("description")
            description = str(desc_raw).strip() if desc_raw else None
            if description == "":
                description = None

            variables = self._parse_variables(item.get("variables"))
            extracted.append(
                ExtractionResult(
                    skill_key=skill_key,
                    description=description,
                    steps=steps,
                    variables=variables,
                )
            )

        return extracted

    async def _call_llm(
        self,
        *,
        segments: list[list[dict[str, Any]]],
        context: ExtractionContext,
    ) -> dict[str, Any]:
        endpoint = f"{self._config.api_base.rstrip('/')}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        prompt_payload = {
            "owner": context.owner,
            "execution_id": context.execution_id,
            "sandbox_id": context.sandbox_id,
            "description": context.description,
            "tags": context.tags,
            "segments": segments,
        }
        request_body = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract browser skill candidates. Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt_payload, ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "skill_extraction_results",
                    "strict": True,
                    "schema": self._response_schema(),
                },
            },
        }

        timeout = httpx.Timeout(self._config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, headers=headers, json=request_body)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid LLM response payload")
            return data

    @staticmethod
    def _parse_variables(raw: Any) -> dict[str, VariableSpec] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("Invalid variables payload")

        variables: dict[str, VariableSpec] = {}
        for name, spec_raw in raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Variable name must be non-empty string")
            if not isinstance(spec_raw, dict):
                raise ValueError("Variable spec must be object")
            variables[name] = VariableSpec(
                type=str(spec_raw["type"]),
                default_value=spec_raw.get("default_value"),
                action_index=int(spec_raw["action_index"]),
                arg_position=int(spec_raw["arg_position"]),
            )
        return variables or None

    async def _fallback_with_reason(
        self,
        *,
        reason: str,
        segments: list[list[dict[str, Any]]],
        context: ExtractionContext,
    ) -> list[ExtractionResult]:
        self._log.warning(
            "skills.browser.extraction.llm_fallback",
            reason=reason,
            execution_id=context.execution_id,
        )
        return await self._fallback.extract(segments=segments, context=context)

    @staticmethod
    def _response_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["results"],
            "additionalProperties": False,
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["skill_key", "steps"],
                        "additionalProperties": False,
                        "properties": {
                            "skill_key": {"type": "string"},
                            "description": {"type": "string"},
                            "steps": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                            "variables": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "object",
                                    "required": [
                                        "type",
                                        "default_value",
                                        "action_index",
                                        "arg_position",
                                    ],
                                    "additionalProperties": False,
                                    "properties": {
                                        "type": {"type": "string"},
                                        "default_value": {},
                                        "action_index": {"type": "integer"},
                                        "arg_position": {"type": "integer"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }

