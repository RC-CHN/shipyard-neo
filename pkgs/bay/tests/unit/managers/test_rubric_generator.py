"""Unit tests for RubricGenerator (Phase 3).

All tests use a FakeLlmClient — no real HTTP calls.

Tests cover:
- generates rubric with all required fields from a goal
- gracefully returns None when LLM call fails
- rubric fields are non-empty strings / non-empty lists
"""

from __future__ import annotations

from app.services.skills.evolution.rubric import RubricGenerator, SkillRubric

# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class _FakeLlmClient:
    """Returns a fixed SkillRubric or raises."""

    def __init__(self, output: SkillRubric | None = None, raises: Exception | None = None):
        self._output = output or SkillRubric(
            summary="Skill returns the exact star count as an integer.",
            success_criteria=[
                "Returns an integer value greater than or equal to 0",
                "Value matches the current star count displayed on the page",
            ],
            failure_indicators=[
                "Returns None or raises an exception",
                "Returns a string instead of integer",
                "Value is 0 for a popular repository",
            ],
            evaluation_focus=(
                "Focus on whether the mutated instructions add explicit waits "
                "and robust selectors that survive page layout changes."
            ),
        )
        self._raises = raises
        self.calls: list[str] = []

    async def generate_rubric(self, goal: str) -> SkillRubric:
        self.calls.append(goal)
        if self._raises is not None:
            raise self._raises
        return self._output


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRubricGenerator:
    def _make_generator(self, llm: _FakeLlmClient | None = None) -> RubricGenerator:
        return RubricGenerator(llm_client=llm or _FakeLlmClient())

    async def test_generates_rubric_for_goal(self):
        gen = self._make_generator()
        rubric = await gen.generate(
            "Navigate to a GitHub repo page and return the current star count as an integer."
        )

        assert rubric is not None
        assert isinstance(rubric, SkillRubric)

    async def test_rubric_summary_is_nonempty(self):
        gen = self._make_generator()
        rubric = await gen.generate("Return the PyPI package version.")

        assert rubric.summary.strip() != ""

    async def test_rubric_success_criteria_nonempty_list(self):
        gen = self._make_generator()
        rubric = await gen.generate("Return the PyPI package version.")

        assert isinstance(rubric.success_criteria, list)
        assert len(rubric.success_criteria) >= 1
        assert all(isinstance(c, str) and c.strip() for c in rubric.success_criteria)

    async def test_rubric_failure_indicators_nonempty_list(self):
        gen = self._make_generator()
        rubric = await gen.generate("Return the PyPI package version.")

        assert isinstance(rubric.failure_indicators, list)
        assert len(rubric.failure_indicators) >= 1

    async def test_rubric_evaluation_focus_nonempty(self):
        gen = self._make_generator()
        rubric = await gen.generate("Return the PyPI package version.")

        assert rubric.evaluation_focus.strip() != ""

    async def test_llm_receives_the_goal_text(self):
        llm = _FakeLlmClient()
        gen = self._make_generator(llm)
        goal = "Fetch the top 10 Hacker News story titles."

        await gen.generate(goal)

        assert len(llm.calls) == 1
        assert llm.calls[0] == goal

    async def test_returns_none_when_llm_fails(self):
        llm = _FakeLlmClient(raises=RuntimeError("LLM unavailable"))
        gen = self._make_generator(llm)

        result = await gen.generate("Some goal")

        assert result is None
