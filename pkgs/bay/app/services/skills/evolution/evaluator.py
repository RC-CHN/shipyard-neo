"""GoalConditionedEvaluator — LLM-judge for mutation candidates.

The evaluator wraps an LLM client that implements:
    async def evaluate_mutation(
        *,
        goal: str,
        rubric_text: str,
        skill_content: str,
        failure_context: str,
    ) -> EvaluationResult

EvaluationResult.score is clamped to [0.0, 1.0] regardless of what the LLM returns.
On any LLM failure the evaluator returns None so callers can degrade gracefully.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.services.skills.evolution.rubric import SkillRubric

logger = structlog.get_logger()


class EvaluationResult(BaseModel):
    """Outcome of a single mutation evaluation."""

    passed: bool
    score: float  # always clamped to [0.0, 1.0]
    reasoning: str

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        object.__setattr__(self, "score", max(0.0, min(1.0, self.score)))


class GoalConditionedEvaluator:
    """Evaluates a mutation candidate against the declared goal and rubric.

    Parameters
    ----------
    llm_client:
        Any object with ``async def evaluate_mutation(...)`` → EvaluationResult.
    """

    def __init__(self, *, llm_client) -> None:
        self._llm = llm_client
        self._log = logger.bind(component="goal_conditioned_evaluator")

    async def evaluate(
        self,
        *,
        goal: str,
        rubric: SkillRubric | None,
        skill_content: str,
        failure_context: str,
    ) -> EvaluationResult | None:
        """Evaluate *skill_content* against *goal*.

        Returns ``None`` on LLM failure so callers can degrade gracefully.
        """
        rubric_text = rubric.to_text() if rubric is not None else "(no rubric provided)"
        try:
            result: EvaluationResult = await self._llm.evaluate_mutation(
                goal=goal,
                rubric_text=rubric_text,
                skill_content=skill_content,
                failure_context=failure_context,
            )
            # Clamp score defensively (EvaluationResult.model_post_init also does this,
            # but the LLM client may return a plain dict or bypass validation)
            clamped = EvaluationResult(
                passed=result.passed,
                score=max(0.0, min(1.0, result.score)),
                reasoning=result.reasoning,
            )
            return clamped
        except Exception as exc:
            self._log.warning(
                "goal_conditioned_evaluator.llm_failed",
                goal=goal[:80],
                error=str(exc),
            )
            return None
