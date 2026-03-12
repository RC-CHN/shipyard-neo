"""Unit tests for SkillMutationAgent (Phase 2 + Phase 3).

All tests use a FakeLlmClient — no real HTTP calls.

Tests cover:
- mutate: happy path creates new candidate with mutation fields set
- mutate: links to parent candidate and meta-prompt
- mutate: stores mutation_reasoning on candidate
- mutate: graceful failure when LLM call fails
- mutate: returns None when no active release exists
- mutate: records meta-prompt usage
Phase 3:
- mutate: includes goal text in LLM prompt when SkillGoal exists
- mutate: calls evaluator after creating candidate when injected
- mutate: auto-promotes when evaluator passes and score >= threshold
- mutate: does not promote when evaluator score < threshold
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.models.skill import ExecutionType, SkillCandidate, SkillCandidateStatus, SkillReleaseStage
from app.services.skills import SkillLifecycleService
from app.services.skills.evolution.agent import MutationOutput, SkillMutationAgent
from app.services.skills.evolution.evaluator import EvaluationResult
from app.services.skills.evolution.meta_prompt import MetaPromptService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def skill_svc(db_session: AsyncSession) -> SkillLifecycleService:
    return SkillLifecycleService(db_session)


@pytest.fixture
def meta_svc(db_session: AsyncSession) -> MetaPromptService:
    return MetaPromptService(db_session)


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class _FakeLlmClient:
    """Synchronously returns a fixed MutationOutput."""

    def __init__(self, output: MutationOutput | None = None, raises: Exception | None = None):
        self._output = output or MutationOutput(
            summary="Improved: wait for dynamic content before reading star count.",
            usage_notes="Use explicit wait after navigation.",
            preconditions=["browser available", "JavaScript enabled"],
            postconditions=["integer returned"],
            mutation_reasoning=(
                "The failure indicated that the star count element loads asynchronously. "
                "Added explicit wait instructions."
            ),
        )
        self._raises = raises
        self.calls: list[str] = []

    async def generate_mutation(self, prompt: str) -> MutationOutput:
        self.calls.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._output


# ---------------------------------------------------------------------------
# Fake evaluator (Phase 3)
# ---------------------------------------------------------------------------


class _FakeEvaluator:
    """Synchronously returns a fixed EvaluationResult and records call kwargs."""

    def __init__(self, output: EvaluationResult | None = None):
        self._output = output or EvaluationResult(passed=True, score=0.85, reasoning="Looks good.")
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def evaluate(
        self,
        *,
        goal: str,
        rubric,
        skill_content: str,
        failure_context: str,
    ) -> EvaluationResult | None:
        self.call_count += 1
        self.last_kwargs = {
            "goal": goal,
            "rubric": rubric,
            "skill_content": skill_content,
            "failure_context": failure_context,
        }
        return self._output


class _FakeRubricGenerator:
    """Returns a fixed rubric string-ish object and records calls."""

    def __init__(self, rubric=None):
        from app.services.skills.evolution.rubric import SkillRubric

        self._rubric = rubric or SkillRubric(
            summary="Return star count as integer.",
            success_criteria=["Returns an integer"],
            failure_indicators=["Returns None"],
            evaluation_focus="Selector robustness.",
        )
        self.call_count = 0

    async def generate(self, goal: str):
        self.call_count += 1
        return self._rubric


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_skill_with_release(
    svc: SkillLifecycleService,
    *,
    owner: str = "default",
    skill_key: str = "github-get-stars",
) -> tuple:
    """Creates candidate → evaluation → release. Returns (candidate, release)."""
    execution = await svc.create_execution(
        owner=owner,
        sandbox_id="sb-1",
        exec_type=ExecutionType.BROWSER,
        code="open github.com",
        success=True,
        execution_time_ms=10,
    )
    candidate = await svc.create_candidate(
        owner=owner,
        skill_key=skill_key,
        source_execution_ids=[execution.id],
        summary="Gets GitHub repo star count",
        usage_notes="Navigate to repo page and read star count",
        preconditions={"browser": "available"},
        postconditions={"result": "integer"},
        created_by="system:test",
    )
    await svc.evaluate_candidate(owner=owner, candidate_id=candidate.id, passed=True, score=0.9)
    release = await svc.promote_candidate(
        owner=owner, candidate_id=candidate.id, stage=SkillReleaseStage.CANARY
    )
    return candidate, release


async def _add_failure_outcomes(
    svc: SkillLifecycleService,
    *,
    owner: str = "default",
    skill_key: str = "github-get-stars",
    release_id: str,
    count: int = 3,
) -> None:
    for i in range(count):
        await svc.record_outcome(
            owner=owner,
            skill_key=skill_key,
            release_id=release_id,
            outcome="failure",
            reasoning=f"Failure #{i + 1}: element not found at expected selector.",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSkillMutationAgent:
    def _make_agent(
        self,
        db_session: AsyncSession,
        llm: _FakeLlmClient | None = None,
        max_recent_outcomes: int = 10,
        evaluator: _FakeEvaluator | None = None,
        auto_promote_threshold: float = 0.7,
        rubric_generator: _FakeRubricGenerator | None = None,
    ) -> SkillMutationAgent:
        return SkillMutationAgent(
            db_session=db_session,
            llm_client=llm or _FakeLlmClient(),
            max_recent_outcomes=max_recent_outcomes,
            evaluator=evaluator,
            auto_promote_threshold=auto_promote_threshold,
            rubric_generator=rubric_generator,
        )

    async def test_mutate_returns_none_when_no_active_release(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        agent = self._make_agent(db_session)

        result = await agent.mutate(owner="default", skill_key="no-such-skill")

        assert result is None

    async def test_mutate_creates_new_candidate(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        agent = self._make_agent(db_session)
        new_candidate_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        assert new_candidate_id is not None

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_candidate_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate is not None
        assert new_candidate.skill_key == "github-get-stars"

    async def test_mutate_links_to_parent_candidate(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        agent = self._make_agent(db_session)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.evolution_parent_id == parent.id

    async def test_mutate_stores_mutation_reasoning(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        llm = _FakeLlmClient()
        agent = self._make_agent(db_session, llm=llm)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.mutation_reasoning is not None
        assert "failure" in new_candidate.mutation_reasoning.lower() or len(
            new_candidate.mutation_reasoning
        ) > 10

    async def test_mutate_applies_llm_output_to_candidate_fields(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        llm = _FakeLlmClient(
            MutationOutput(
                summary="Wait for dynamic content",
                usage_notes="Use explicit wait",
                preconditions=["browser available"],
                postconditions=["integer returned"],
                mutation_reasoning="Added wait for async loading.",
            )
        )
        agent = self._make_agent(db_session, llm=llm)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.summary == "Wait for dynamic content"
        assert new_candidate.usage_notes == "Use explicit wait"

    async def test_mutate_stores_meta_prompt_id(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        agent = self._make_agent(db_session)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.evolution_meta_prompt_id is not None

    async def test_mutate_marks_candidate_created_by_evolution(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        agent = self._make_agent(db_session)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.created_by == "system:evolution"

    async def test_mutate_calls_llm_with_context_including_failures(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id, count=2)

        llm = _FakeLlmClient()
        agent = self._make_agent(db_session, llm=llm)
        await agent.mutate(owner="default", skill_key="github-get-stars")

        assert len(llm.calls) == 1
        prompt = llm.calls[0]
        # prompt should contain failure context
        assert "failure" in prompt.lower() or "Failure" in prompt

    async def test_mutate_returns_none_and_logs_when_llm_fails(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        llm = _FakeLlmClient(raises=RuntimeError("LLM unavailable"))
        agent = self._make_agent(db_session, llm=llm)

        result = await agent.mutate(owner="default", skill_key="github-get-stars")

        assert result is None

    async def test_mutate_records_meta_prompt_usage(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)

        agent = self._make_agent(db_session)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        prompt_id = new_candidate.evolution_meta_prompt_id

        updated_prompt = await meta_svc.get_prompt(prompt_id)
        assert updated_prompt.usage_count >= 1

    # -------------------------------------------------------------------------
    # Phase 3: Goal-conditioned evolution
    # -------------------------------------------------------------------------

    async def test_mutate_includes_goal_in_llm_prompt(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """When a SkillGoal is declared, the goal text appears in the LLM prompt."""
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return the exact star count as an integer.",
        )

        llm = _FakeLlmClient()
        agent = self._make_agent(db_session, llm=llm)
        await agent.mutate(owner="default", skill_key="github-get-stars")

        assert len(llm.calls) == 1
        assert "Return the exact star count as an integer." in llm.calls[0]

    async def test_mutate_calls_evaluator_after_creating_candidate(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """When an evaluator is injected, it's invoked after the mutation candidate is created."""
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return star count.",
        )

        evaluator = _FakeEvaluator(EvaluationResult(passed=True, score=0.9, reasoning="Good."))
        agent = self._make_agent(db_session, evaluator=evaluator)
        await agent.mutate(owner="default", skill_key="github-get-stars")

        assert evaluator.call_count == 1

    async def test_mutate_auto_promotes_when_evaluator_passes(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """If evaluator returns passed=True with score >= threshold, candidate is auto-promoted."""
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return star count.",
        )

        evaluator = _FakeEvaluator(EvaluationResult(passed=True, score=0.9, reasoning="Excellent."))
        agent = self._make_agent(db_session, evaluator=evaluator, auto_promote_threshold=0.7)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.status == SkillCandidateStatus.PROMOTED

    async def test_mutate_does_not_promote_when_score_below_threshold(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """Candidate stays DRAFT when evaluator score is below the auto-promote threshold."""
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return star count.",
        )

        evaluator = _FakeEvaluator(EvaluationResult(passed=False, score=0.4, reasoning="Too slow."))
        agent = self._make_agent(db_session, evaluator=evaluator, auto_promote_threshold=0.7)
        new_id = await agent.mutate(owner="default", skill_key="github-get-stars")

        result = await db_session.execute(
            select(SkillCandidate).where(SkillCandidate.id == new_id)
        )
        new_candidate = result.scalars().first()
        assert new_candidate.status == SkillCandidateStatus.DRAFT

    async def test_mutate_evaluator_receives_mutated_content_not_parent(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """Evaluator must see the mutation output fields, not the parent candidate's fields."""
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return star count.",
        )

        # LLM produces a mutation with a distinctive summary
        mutated_summary = "MUTATED: wait 3s for dynamic star count element"
        llm = _FakeLlmClient(
            MutationOutput(
                summary=mutated_summary,
                usage_notes="Use explicit wait",
                preconditions=["browser available"],
                postconditions=["integer returned"],
                mutation_reasoning="Addressed async loading failure.",
            )
        )
        evaluator = _FakeEvaluator(EvaluationResult(passed=True, score=0.9, reasoning="Good."))
        agent = self._make_agent(db_session, llm=llm, evaluator=evaluator)
        await agent.mutate(owner="default", skill_key="github-get-stars")

        # The skill_content passed to evaluator must contain the mutated summary
        assert evaluator.call_count == 1
        assert mutated_summary in evaluator.last_kwargs["skill_content"]
        # And must NOT be the parent's summary
        assert "Gets GitHub repo star count" not in evaluator.last_kwargs["skill_content"]

    async def test_mutate_evaluator_receives_rubric_when_generator_provided(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """When a rubric_generator is injected, the evaluator receives the generated rubric."""
        await meta_svc.seed_defaults()
        parent, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return star count.",
        )

        rubric_gen = _FakeRubricGenerator()
        evaluator = _FakeEvaluator(EvaluationResult(passed=True, score=0.9, reasoning="Good."))
        agent = self._make_agent(db_session, evaluator=evaluator, rubric_generator=rubric_gen)
        await agent.mutate(owner="default", skill_key="github-get-stars")

        assert evaluator.call_count == 1
        assert evaluator.last_kwargs["rubric"] is not None
        assert rubric_gen.call_count >= 1

    async def test_mutate_backfills_rubric_summary_from_cached_rubric_json(
        self,
        db_session: AsyncSession,
        skill_svc: SkillLifecycleService,
        meta_svc: MetaPromptService,
    ):
        """Legacy cached rubrics should self-heal rubric_summary on read."""
        await meta_svc.seed_defaults()
        _, release = await _setup_skill_with_release(skill_svc)
        await _add_failure_outcomes(skill_svc, release_id=release.id)
        goal = await skill_svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return star count.",
        )
        goal.rubric_json = (
            '{"summary":"Cached rubric summary","success_criteria":["Returns an integer"],'
            '"failure_indicators":["Returns None"],"evaluation_focus":"Selector robustness."}'
        )
        goal.rubric_summary = ""
        db_session.add(goal)
        await db_session.commit()

        rubric_gen = _FakeRubricGenerator()
        evaluator = _FakeEvaluator(EvaluationResult(passed=True, score=0.9, reasoning="Good."))
        agent = self._make_agent(db_session, evaluator=evaluator, rubric_generator=rubric_gen)
        await agent.mutate(owner="default", skill_key="github-get-stars")

        updated_goal = await skill_svc.get_skill_goal(
            owner="default",
            skill_key="github-get-stars",
        )
        assert evaluator.call_count == 1
        assert evaluator.last_kwargs["rubric"] is not None
        assert evaluator.last_kwargs["rubric"].summary == "Cached rubric summary"
        assert updated_goal is not None
        assert updated_goal.rubric_summary == "Cached rubric summary"
        assert rubric_gen.call_count == 0
