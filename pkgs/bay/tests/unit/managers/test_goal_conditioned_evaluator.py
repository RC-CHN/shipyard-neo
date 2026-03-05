"""Unit tests for GoalConditionedEvaluator (Phase 3).

All tests use a FakeLlmClient — no real HTTP calls.

Tests cover:
- evaluates passing mutation candidate
- evaluates failing mutation candidate
- includes goal, rubric, and failure context in the LLM prompt
- returns None (graceful failure) when LLM call fails
- score is clamped to [0.0, 1.0]
"""

from __future__ import annotations

import pytest

from app.services.skills.evolution.evaluator import EvaluationResult, GoalConditionedEvaluator
from app.services.skills.evolution.rubric import SkillRubric

# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class _FakeLlmClient:
    def __init__(
        self,
        output: EvaluationResult | None = None,
        raises: Exception | None = None,
    ):
        self._output = output or EvaluationResult(
            passed=True,
            score=0.85,
            reasoning=(
                "The mutation adds explicit waits and semantic selectors that address "
                "the reported async rendering failures."
            ),
        )
        self._raises = raises
        self.calls: list[dict] = []  # records kwargs passed to evaluate

    async def evaluate_mutation(
        self,
        *,
        goal: str,
        rubric_text: str,
        skill_content: str,
        failure_context: str,
    ) -> EvaluationResult:
        self.calls.append(
            {
                "goal": goal,
                "rubric_text": rubric_text,
                "skill_content": skill_content,
                "failure_context": failure_context,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._output


_SAMPLE_RUBRIC = SkillRubric(
    summary="Skill returns star count as an integer.",
    success_criteria=["Returns an integer", "Handles async loading"],
    failure_indicators=["Returns None", "Timeout"],
    evaluation_focus="Check robustness of selectors and wait strategies.",
)

_SAMPLE_CONTENT = "# SKILL: github-get-stars\n\n## Summary\nImproved wait logic."
_SAMPLE_FAILURES = "- Element not found\n- Timeout after 3s"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGoalConditionedEvaluator:
    def _make_evaluator(self, llm: _FakeLlmClient | None = None) -> GoalConditionedEvaluator:
        return GoalConditionedEvaluator(llm_client=llm or _FakeLlmClient())

    async def test_returns_evaluation_result(self):
        ev = self._make_evaluator()
        result = await ev.evaluate(
            goal="Return star count as integer.",
            rubric=_SAMPLE_RUBRIC,
            skill_content=_SAMPLE_CONTENT,
            failure_context=_SAMPLE_FAILURES,
        )

        assert isinstance(result, EvaluationResult)

    async def test_passing_candidate_returns_passed_true(self):
        llm = _FakeLlmClient(EvaluationResult(passed=True, score=0.9, reasoning="Good."))
        ev = self._make_evaluator(llm)

        result = await ev.evaluate(
            goal="Return star count.",
            rubric=_SAMPLE_RUBRIC,
            skill_content=_SAMPLE_CONTENT,
            failure_context=_SAMPLE_FAILURES,
        )

        assert result.passed is True
        assert result.score == pytest.approx(0.9)

    async def test_failing_candidate_returns_passed_false(self):
        llm = _FakeLlmClient(EvaluationResult(passed=False, score=0.2, reasoning="Still broken."))
        ev = self._make_evaluator(llm)

        result = await ev.evaluate(
            goal="Return star count.",
            rubric=_SAMPLE_RUBRIC,
            skill_content=_SAMPLE_CONTENT,
            failure_context=_SAMPLE_FAILURES,
        )

        assert result.passed is False
        assert result.score < 0.5

    async def test_goal_included_in_llm_call(self):
        llm = _FakeLlmClient()
        ev = self._make_evaluator(llm)
        goal = "Return the exact star count as an integer."

        await ev.evaluate(
            goal=goal,
            rubric=_SAMPLE_RUBRIC,
            skill_content=_SAMPLE_CONTENT,
            failure_context=_SAMPLE_FAILURES,
        )

        assert len(llm.calls) == 1
        assert goal in llm.calls[0]["goal"]

    async def test_failure_context_included_in_llm_call(self):
        llm = _FakeLlmClient()
        ev = self._make_evaluator(llm)

        await ev.evaluate(
            goal="Return star count.",
            rubric=_SAMPLE_RUBRIC,
            skill_content=_SAMPLE_CONTENT,
            failure_context="- Element not found at .star-count",
        )

        assert "Element not found" in llm.calls[0]["failure_context"]

    async def test_works_without_rubric(self):
        ev = self._make_evaluator()
        result = await ev.evaluate(
            goal="Return star count.",
            rubric=None,
            skill_content=_SAMPLE_CONTENT,
            failure_context=_SAMPLE_FAILURES,
        )

        assert result is not None

    async def test_returns_none_when_llm_fails(self):
        llm = _FakeLlmClient(raises=RuntimeError("LLM unavailable"))
        ev = self._make_evaluator(llm)

        result = await ev.evaluate(
            goal="Return star count.",
            rubric=_SAMPLE_RUBRIC,
            skill_content=_SAMPLE_CONTENT,
            failure_context=_SAMPLE_FAILURES,
        )

        assert result is None

    async def test_score_clamped_to_zero_one(self):
        llm = _FakeLlmClient(EvaluationResult(passed=True, score=1.5, reasoning="Overclaim."))
        ev = self._make_evaluator(llm)

        result = await ev.evaluate(
            goal="g", rubric=None, skill_content="c", failure_context="f"
        )

        assert 0.0 <= result.score <= 1.0
