"""Unit tests for EvolutionScheduler (Phase 2).

Uses a fake SkillMutationAgent — no real LLM calls.

Tests cover:
- cycle skips when disabled
- cycle triggers mutation for skills with enough failures
- cycle respects max_mutations_per_cycle budget
- cycle skips skills with too few failures
- cycle result counters are correct
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.config import EvolutionConfig
from app.models.skill import ExecutionType, SkillCandidate, SkillCandidateStatus, SkillReleaseStage
from app.services.skills import SkillLifecycleService
from app.services.skills.evolution.scheduler import EvolutionScheduler
from app.utils.datetime import utcnow

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


# ---------------------------------------------------------------------------
# Fake mutation agent
# ---------------------------------------------------------------------------


class _FakeMutationAgent:
    """Records mutate() calls.

    If ``db_session`` is provided, creates a real SkillCandidate row so the
    scheduler's dedup check (which queries the DB) works correctly.
    """

    def __init__(self, *, success: bool = True, db_session: AsyncSession | None = None):
        self.calls: list[tuple[str, str]] = []  # (owner, skill_key)
        self._success = success
        self._db = db_session

    async def mutate(self, *, owner: str, skill_key: str) -> str | None:
        self.calls.append((owner, skill_key))
        if not self._success:
            return None
        candidate_id = f"sc-fake-{uuid.uuid4().hex[:8]}"
        if self._db is not None:
            candidate = SkillCandidate(
                id=candidate_id,
                owner=owner,
                skill_key=skill_key,
                source_execution_ids="",
                status=SkillCandidateStatus.DRAFT,
                created_by="system:evolution",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            self._db.add(candidate)
            await self._db.commit()
        return candidate_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_skill_with_release_and_failures(
    svc: SkillLifecycleService,
    *,
    owner: str = "default",
    skill_key: str,
    failure_count: int,
) -> str:
    """Returns release_id."""
    execution = await svc.create_execution(
        owner=owner,
        sandbox_id="sb-1",
        exec_type=ExecutionType.BROWSER,
        code="open about:blank",
        success=True,
        execution_time_ms=5,
    )
    candidate = await svc.create_candidate(
        owner=owner,
        skill_key=skill_key,
        source_execution_ids=[execution.id],
        summary="Test skill",
        created_by="system:test",
    )
    await svc.evaluate_candidate(owner=owner, candidate_id=candidate.id, passed=True, score=0.9)
    release = await svc.promote_candidate(
        owner=owner, candidate_id=candidate.id, stage=SkillReleaseStage.CANARY
    )

    # Declare goal so scheduler knows about the skill
    await svc.declare_goal(owner=owner, skill_key=skill_key, goal=f"Goal for {skill_key}")

    for i in range(failure_count):
        await svc.record_outcome(
            owner=owner,
            skill_key=skill_key,
            release_id=release.id,
            outcome="failure",
            reasoning=f"Failure {i + 1}",
        )

    return release.id


def _make_scheduler(
    db_session: AsyncSession,
    agent: _FakeMutationAgent,
    *,
    enabled: bool = True,
    min_failures: int = 2,
    max_mutations: int = 5,
) -> EvolutionScheduler:
    config = EvolutionConfig(
        enabled=enabled,
        min_failures_to_trigger=min_failures,
        max_mutations_per_cycle=max_mutations,
        max_recent_outcomes=10,
    )
    return EvolutionScheduler(
        db_session=db_session,
        config=config,
        mutation_agent=agent,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvolutionScheduler:
    async def test_cycle_returns_empty_when_disabled(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        agent = _FakeMutationAgent()
        scheduler = _make_scheduler(db_session, agent, enabled=False)

        result = await scheduler.run_cycle()

        assert result.mutations_attempted == 0
        assert result.mutations_succeeded == 0
        assert len(agent.calls) == 0

    async def test_cycle_triggers_mutation_for_skill_with_enough_failures(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        await _setup_skill_with_release_and_failures(
            skill_svc, skill_key="failing-skill", failure_count=3
        )

        agent = _FakeMutationAgent()
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        result = await scheduler.run_cycle()

        assert any(call[1] == "failing-skill" for call in agent.calls)
        assert result.mutations_attempted >= 1

    async def test_cycle_skips_skill_with_too_few_failures(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        await _setup_skill_with_release_and_failures(
            skill_svc, skill_key="barely-failing", failure_count=1
        )

        agent = _FakeMutationAgent()
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        await scheduler.run_cycle()

        assert not any(call[1] == "barely-failing" for call in agent.calls)

    async def test_cycle_skips_skill_with_no_goal_declared(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        """Skills without a declared goal are not in the evolution loop."""
        execution = await skill_svc.create_execution(
            owner="default",
            sandbox_id="sb-1",
            exec_type=ExecutionType.BROWSER,
            code="open about:blank",
            success=True,
            execution_time_ms=5,
        )
        candidate = await skill_svc.create_candidate(
            owner="default",
            skill_key="no-goal-skill",
            source_execution_ids=[execution.id],
            created_by="system:test",
        )
        await skill_svc.evaluate_candidate(
            owner="default", candidate_id=candidate.id, passed=True
        )
        release = await skill_svc.promote_candidate(
            owner="default", candidate_id=candidate.id, stage=SkillReleaseStage.CANARY
        )
        # Add failures but NO goal declaration
        for _ in range(5):
            await skill_svc.record_outcome(
                owner="default",
                skill_key="no-goal-skill",
                release_id=release.id,
                outcome="failure",
                reasoning="failed",
            )

        agent = _FakeMutationAgent()
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        await scheduler.run_cycle()

        assert not any(call[1] == "no-goal-skill" for call in agent.calls)

    async def test_cycle_respects_max_mutations_budget(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        for i in range(5):
            await _setup_skill_with_release_and_failures(
                skill_svc, skill_key=f"skill-{i}", failure_count=3
            )

        agent = _FakeMutationAgent()
        scheduler = _make_scheduler(db_session, agent, min_failures=2, max_mutations=2)

        result = await scheduler.run_cycle()

        assert result.mutations_attempted <= 2
        assert len(agent.calls) <= 2

    async def test_cycle_counts_succeeded_mutations(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        await _setup_skill_with_release_and_failures(
            skill_svc, skill_key="success-skill", failure_count=3
        )

        agent = _FakeMutationAgent(success=True)
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        result = await scheduler.run_cycle()

        assert result.mutations_succeeded >= 1

    async def test_cycle_counts_failed_mutations(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        await _setup_skill_with_release_and_failures(
            skill_svc, skill_key="fail-skill", failure_count=3
        )

        agent = _FakeMutationAgent(success=False)
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        result = await scheduler.run_cycle()

        assert result.mutations_attempted >= 1
        assert result.mutations_succeeded == 0

    async def test_cycle_skips_skill_already_mutated_since_last_failure(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        """Skip mutating when an evolution candidate already exists after the latest failure."""
        await _setup_skill_with_release_and_failures(
            skill_svc, skill_key="stable-skill", failure_count=3
        )

        # Pass db_session so the fake agent creates a real SkillCandidate row
        agent = _FakeMutationAgent(success=True, db_session=db_session)
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        # First cycle mutates
        first = await scheduler.run_cycle()
        assert first.mutations_attempted == 1

        # Second cycle: mutation candidate already exists after latest failure → skip
        second = await scheduler.run_cycle()
        assert second.mutations_attempted == 0
        assert len(agent.calls) == 1  # agent called exactly once total

    async def test_cycle_mutates_again_after_new_failures(
        self, db_session: AsyncSession, skill_svc: SkillLifecycleService
    ):
        """New failures that arrive after the last mutation should re-trigger evolution."""
        release_id = await _setup_skill_with_release_and_failures(
            skill_svc, skill_key="resurging-skill", failure_count=3
        )

        agent = _FakeMutationAgent(success=True, db_session=db_session)
        scheduler = _make_scheduler(db_session, agent, min_failures=2)

        # First cycle mutates
        await scheduler.run_cycle()
        assert len(agent.calls) == 1

        # New failures arrive after the mutation candidate was created
        for i in range(3):
            await skill_svc.record_outcome(
                owner="default",
                skill_key="resurging-skill",
                release_id=release_id,
                outcome="failure",
                reasoning=f"New post-mutation failure {i}",
            )

        # Second cycle: new failures detected → mutate again
        second = await scheduler.run_cycle()
        assert second.mutations_attempted == 1
        assert len(agent.calls) == 2
