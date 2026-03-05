"""LLM client for skill mutation — thin, mockable boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.config import LlmEvolutionConfig

logger = structlog.get_logger()


@dataclass
class MutationOutput:
    """Structured output from the LLM mutation call."""

    summary: str
    usage_notes: str
    preconditions: list[str]
    postconditions: list[str]
    mutation_reasoning: str


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "usage_notes", "preconditions", "postconditions", "mutation_reasoning"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "usage_notes": {"type": "string"},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "postconditions": {"type": "array", "items": {"type": "string"}},
        "mutation_reasoning": {"type": "string"},
    },
}

_SYSTEM_PROMPT = """\
You are an expert at improving AI agent skill instructions.
You receive:
1. The current skill content (summary, usage notes, pre/postconditions)
2. Recent failure reports from executions of this skill
3. A mutation strategy to apply

Your task: rewrite the skill content to address the failures, applying the given strategy.
Return strict JSON only. The mutation_reasoning field MUST explain specifically what you changed and why.
No markdown, no explanation outside the JSON object.\
"""


class LlmEvolutionClient:
    """OpenAI-compatible HTTP client for skill mutation."""

    def __init__(self, config: LlmEvolutionConfig) -> None:
        self._config = config
        self._log = logger.bind(component="llm_evolution_client")

    async def generate_mutation(self, prompt: str) -> MutationOutput:
        endpoint = f"{self._config.api_base.rstrip('/')}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        request_body = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "skill_mutation_output",
                    "strict": True,
                    "schema": _RESPONSE_SCHEMA,
                },
            },
        }

        timeout = httpx.Timeout(self._config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, headers=headers, json=request_body)
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )

        parsed = json.loads(content)
        return MutationOutput(
            summary=str(parsed["summary"]),
            usage_notes=str(parsed["usage_notes"]),
            preconditions=[str(p) for p in parsed.get("preconditions", [])],
            postconditions=[str(p) for p in parsed.get("postconditions", [])],
            mutation_reasoning=str(parsed["mutation_reasoning"]),
        )
