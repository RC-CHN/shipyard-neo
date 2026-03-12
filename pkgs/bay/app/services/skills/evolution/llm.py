"""LLM client for skill evolution — thin, mockable boundary.

Three tasks, one HTTP interface:
- generate_mutation   → SkillMutationAgent
- generate_rubric     → RubricGenerator
- evaluate_mutation   → GoalConditionedEvaluator

Each task can use a different model via per-task overrides in LlmEvolutionConfig.
Use ``make_rubric_client`` / ``make_evaluator_client`` to get task-specific clients.
"""

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
Return strict JSON only. The mutation_reasoning field MUST explain specifically what you changed \
and why.
No markdown, no explanation outside the JSON object.\
"""


_RUBRIC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "success_criteria", "failure_indicators", "evaluation_focus"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "failure_indicators": {"type": "array", "items": {"type": "string"}},
        "evaluation_focus": {"type": "string"},
    },
}

_RUBRIC_SYSTEM_PROMPT = """\
You are an expert at writing evaluation rubrics for AI agent skills.
You receive a natural language goal for a skill.
Your task: create a structured rubric with clear success criteria and failure indicators.
Return strict JSON only matching the schema. Be concise and specific.\
"""

_EVAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["passed", "score", "reasoning"],
    "additionalProperties": False,
    "properties": {
        "passed": {"type": "boolean"},
        "score": {"type": "number"},
        "reasoning": {"type": "string"},
    },
}

_EVAL_SYSTEM_PROMPT = """\
You are an expert evaluator of AI agent skill instructions.
You receive:
1. The goal the skill must satisfy
2. An evaluation rubric (success criteria, failure indicators, evaluation focus)
3. The current skill content (the mutated candidate)
4. Recent failure context from previous executions

Your task: judge whether the mutated skill content is likely to satisfy the goal.
Return strict JSON only.
- passed: true only if the mutation clearly addresses the stated goal and rubric
- score: float between 0.0 (completely wrong) and 1.0 (perfectly correct)
- reasoning: 1-3 sentences explaining your verdict\
"""


class LlmEvolutionClient:
    """OpenAI-compatible HTTP client for skill evolution tasks."""

    def __init__(
        self,
        config: LlmEvolutionConfig,
        *,
        model_override: str | None = None,
        api_base_override: str | None = None,
        api_key_override: str | None = None,
    ) -> None:
        self._config = config
        self._model = model_override or config.model
        self._api_base = api_base_override or config.api_base
        self._api_key = api_key_override if api_key_override is not None else config.api_key
        self._log = logger.bind(component="llm_evolution_client")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _post_json_schema(
        self,
        prompt: str,
        system: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        endpoint = f"{self._api_base.rstrip('/')}/chat/completions"
        request_body = {
            "model": self._model,
            "max_tokens": self._config.max_tokens,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        timeout = httpx.Timeout(self._config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, headers=self._headers(), json=request_body)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        return json.loads(content)

    async def generate_mutation(self, prompt: str) -> MutationOutput:
        parsed = await self._post_json_schema(
            prompt=prompt,
            system=_SYSTEM_PROMPT,
            schema=_RESPONSE_SCHEMA,
            schema_name="skill_mutation_output",
        )
        return MutationOutput(
            summary=str(parsed["summary"]),
            usage_notes=str(parsed["usage_notes"]),
            preconditions=[str(p) for p in parsed.get("preconditions", [])],
            postconditions=[str(p) for p in parsed.get("postconditions", [])],
            mutation_reasoning=str(parsed["mutation_reasoning"]),
        )

    async def generate_rubric(self, goal: str) -> "SkillRubric":  # noqa: F821
        """Generate a SkillRubric from a natural language goal."""
        from app.services.skills.evolution.rubric import SkillRubric  # avoid circular

        parsed = await self._post_json_schema(
            prompt=f"Goal: {goal}",
            system=_RUBRIC_SYSTEM_PROMPT,
            schema=_RUBRIC_RESPONSE_SCHEMA,
            schema_name="skill_rubric_output",
        )
        return SkillRubric(
            summary=str(parsed["summary"]),
            success_criteria=[str(c) for c in parsed.get("success_criteria", [])],
            failure_indicators=[str(f) for f in parsed.get("failure_indicators", [])],
            evaluation_focus=str(parsed["evaluation_focus"]),
        )

    async def evaluate_mutation(
        self,
        *,
        goal: str,
        rubric_text: str,
        skill_content: str,
        failure_context: str,
    ) -> "EvaluationResult":  # noqa: F821
        """Judge a mutated skill against goal and rubric."""
        from app.services.skills.evolution.evaluator import EvaluationResult  # avoid circular

        user_prompt = (
            f"## Goal\n{goal}\n\n"
            f"## Rubric\n{rubric_text}\n\n"
            f"## Skill Content\n{skill_content}\n\n"
            f"## Recent Failures\n{failure_context}"
        )
        parsed = await self._post_json_schema(
            prompt=user_prompt,
            system=_EVAL_SYSTEM_PROMPT,
            schema=_EVAL_RESPONSE_SCHEMA,
            schema_name="skill_evaluation_output",
        )
        return EvaluationResult(
            passed=bool(parsed["passed"]),
            score=float(parsed["score"]),
            reasoning=str(parsed["reasoning"]),
        )


def make_rubric_client(config: LlmEvolutionConfig) -> LlmEvolutionClient:
    """Return an LlmEvolutionClient configured for rubric generation."""
    return LlmEvolutionClient(
        config,
        model_override=config.rubric_model,
        api_base_override=config.rubric_api_base,
        api_key_override=config.rubric_api_key,
    )


def make_evaluator_client(config: LlmEvolutionConfig) -> LlmEvolutionClient:
    """Return an LlmEvolutionClient configured for mutation evaluation."""
    return LlmEvolutionClient(
        config,
        model_override=config.evaluator_model,
        api_base_override=config.evaluator_api_base,
        api_key_override=config.evaluator_api_key,
    )
