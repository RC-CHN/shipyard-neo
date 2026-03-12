"""SkillRubric dataclass and RubricGenerator.

RubricGenerator wraps an LLM client that implements:
    async def generate_rubric(goal: str) -> SkillRubric

It is intentionally thin — all strategy lives in the LLM client,
which is constructed in lifecycle.py using per-task model overrides.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class SkillRubric(BaseModel):
    """Structured rubric for goal-conditioned evaluation.

    Generated once per SkillGoal and cached in SkillGoal.rubric_json.
    """

    summary: str
    success_criteria: list[str]
    failure_indicators: list[str]
    evaluation_focus: str

    def to_text(self) -> str:
        """Render rubric as a plain-text block for inclusion in LLM prompts."""
        lines = [
            f"Summary: {self.summary}",
            "",
            "Success criteria:",
            *[f"  - {c}" for c in self.success_criteria],
            "",
            "Failure indicators:",
            *[f"  - {f}" for f in self.failure_indicators],
            "",
            f"Evaluation focus: {self.evaluation_focus}",
        ]
        return "\n".join(lines)


class RubricGenerator:
    """Generates a SkillRubric from a natural language goal via an LLM client.

    Parameters
    ----------
    llm_client:
        Any object with ``async def generate_rubric(goal: str) -> SkillRubric``.
    """

    def __init__(self, *, llm_client) -> None:
        self._llm = llm_client
        self._log = logger.bind(component="rubric_generator")

    async def generate(self, goal: str) -> SkillRubric | None:
        """Generate a rubric for *goal*.

        Returns ``None`` on any LLM failure so callers can proceed without one.
        """
        try:
            rubric: SkillRubric = await self._llm.generate_rubric(goal)
            return rubric
        except Exception as exc:
            self._log.warning("rubric_generator.llm_failed", goal=goal[:80], error=str(exc))
            return None
