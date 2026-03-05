"""Unit tests for skill evolution service methods (Phase 1).

Tests cover:
- declare_goal: create, upsert, owner isolation
- get_skill_goal: returns None when absent
- record_outcome: success/failure/partial, invalid outcome, signals
- get_active_skill_view: no release, with release + goal, content assembly
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.errors import ValidationError
from app.models.skill import (
    ExecutionType,
    SkillCandidateStatus,
    SkillReleaseStage,
    SkillType,
)
from app.services.skills import SkillLifecycleService


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
def svc(db_session: AsyncSession) -> SkillLifecycleService:
    return SkillLifecycleService(db_session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_candidate_with_release(
    svc: SkillLifecycleService,
    *,
    owner: str = "default",
    skill_key: str = "test-skill",
    summary: str | None = "Test summary",
    usage_notes: str | None = "Test usage notes",
    preconditions: dict | None = None,
    postconditions: dict | None = None,
) -> tuple:
    """Create a candidate + passing evaluation + active release. Returns (candidate, release)."""
    execution = await svc.create_execution(
        owner=owner,
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER,
        code="open about:blank",
        success=True,
        execution_time_ms=10,
    )
    candidate = await svc.create_candidate(
        owner=owner,
        skill_key=skill_key,
        source_execution_ids=[execution.id],
        summary=summary,
        usage_notes=usage_notes,
        preconditions=preconditions,
        postconditions=postconditions,
        created_by="system:test",
    )
    await svc.evaluate_candidate(
        owner=owner,
        candidate_id=candidate.id,
        passed=True,
        score=0.9,
    )
    release = await svc.promote_candidate(
        owner=owner,
        candidate_id=candidate.id,
        stage=SkillReleaseStage.CANARY,
    )
    return candidate, release


# ---------------------------------------------------------------------------
# declare_goal
# ---------------------------------------------------------------------------


class TestDeclareGoal:
    async def test_creates_new_record(self, svc: SkillLifecycleService):
        goal = await svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Return the star count of a GitHub repo.",
        )

        assert goal.id.startswith("goal-")
        assert goal.skill_key == "github-get-stars"
        assert goal.goal == "Return the star count of a GitHub repo."
        assert goal.owner == "default"
        assert goal.rubric_summary == ""

    async def test_upserts_existing_goal(self, svc: SkillLifecycleService):
        first = await svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Original goal.",
        )
        second = await svc.declare_goal(
            owner="default",
            skill_key="github-get-stars",
            goal="Updated goal.",
        )

        assert second.id == first.id
        assert second.goal == "Updated goal."

    async def test_owner_isolation(self, svc: SkillLifecycleService):
        goal_a = await svc.declare_goal(
            owner="alice",
            skill_key="shared-skill",
            goal="Alice's goal.",
        )
        goal_b = await svc.declare_goal(
            owner="bob",
            skill_key="shared-skill",
            goal="Bob's goal.",
        )

        assert goal_a.id != goal_b.id
        assert goal_a.goal == "Alice's goal."
        assert goal_b.goal == "Bob's goal."

    async def test_different_skill_keys_are_independent(self, svc: SkillLifecycleService):
        g1 = await svc.declare_goal(owner="default", skill_key="skill-a", goal="Goal A")
        g2 = await svc.declare_goal(owner="default", skill_key="skill-b", goal="Goal B")

        assert g1.id != g2.id
        assert g1.skill_key == "skill-a"
        assert g2.skill_key == "skill-b"


# ---------------------------------------------------------------------------
# get_skill_goal
# ---------------------------------------------------------------------------


class TestGetSkillGoal:
    async def test_returns_none_when_not_declared(self, svc: SkillLifecycleService):
        result = await svc.get_skill_goal(owner="default", skill_key="nonexistent")
        assert result is None

    async def test_returns_goal_after_declare(self, svc: SkillLifecycleService):
        await svc.declare_goal(owner="default", skill_key="found-skill", goal="A goal")
        result = await svc.get_skill_goal(owner="default", skill_key="found-skill")

        assert result is not None
        assert result.goal == "A goal"

    async def test_owner_isolation_on_get(self, svc: SkillLifecycleService):
        await svc.declare_goal(owner="alice", skill_key="skill-x", goal="Alice goal")

        result = await svc.get_skill_goal(owner="bob", skill_key="skill-x")
        assert result is None


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------


class TestRecordOutcome:
    async def test_success_outcome(self, svc: SkillLifecycleService):
        outcome = await svc.record_outcome(
            owner="default",
            skill_key="github-get-stars",
            release_id="release-abc",
            outcome="success",
            reasoning="Page loaded, star count found at expected selector.",
        )

        assert outcome.id.startswith("outcome-")
        assert outcome.owner == "default"
        assert outcome.skill_key == "github-get-stars"
        assert outcome.release_id == "release-abc"
        assert outcome.outcome == "success"
        assert outcome.reasoning == "Page loaded, star count found at expected selector."
        assert outcome.execution_id is None
        assert outcome.signals_json is None

    async def test_failure_outcome_with_signals(self, svc: SkillLifecycleService):
        signals = {"page_load_time_ms": 5000, "element_found": False}
        outcome = await svc.record_outcome(
            owner="default",
            skill_key="github-get-stars",
            release_id="release-xyz",
            outcome="failure",
            reasoning="Star count element no longer at .social-count — layout changed.",
            execution_id="exec-123",
            signals=signals,
        )

        assert outcome.outcome == "failure"
        assert outcome.execution_id == "exec-123"
        parsed_signals = json.loads(outcome.signals_json)
        assert parsed_signals["element_found"] is False
        assert parsed_signals["page_load_time_ms"] == 5000

    async def test_partial_outcome(self, svc: SkillLifecycleService):
        outcome = await svc.record_outcome(
            owner="default",
            skill_key="ui-beautify",
            release_id="release-001",
            outcome="partial",
            reasoning="UI partially updated; footer styles were skipped.",
        )

        assert outcome.outcome == "partial"

    async def test_invalid_outcome_raises_validation_error(self, svc: SkillLifecycleService):
        with pytest.raises(ValidationError, match="Invalid outcome"):
            await svc.record_outcome(
                owner="default",
                skill_key="test-skill",
                release_id="release-001",
                outcome="unknown",
                reasoning="some reasoning",
            )

    async def test_multiple_outcomes_are_independent_records(self, svc: SkillLifecycleService):
        o1 = await svc.record_outcome(
            owner="default",
            skill_key="skill-z",
            release_id="rel-1",
            outcome="success",
            reasoning="First run OK.",
        )
        o2 = await svc.record_outcome(
            owner="default",
            skill_key="skill-z",
            release_id="rel-1",
            outcome="failure",
            reasoning="Second run failed.",
        )

        assert o1.id != o2.id

    async def test_owner_isolation(self, svc: SkillLifecycleService):
        oa = await svc.record_outcome(
            owner="alice",
            skill_key="shared-skill",
            release_id="rel-1",
            outcome="success",
            reasoning="Alice's run.",
        )
        ob = await svc.record_outcome(
            owner="bob",
            skill_key="shared-skill",
            release_id="rel-1",
            outcome="failure",
            reasoning="Bob's run.",
        )

        assert oa.owner == "alice"
        assert ob.owner == "bob"


# ---------------------------------------------------------------------------
# get_active_skill_view
# ---------------------------------------------------------------------------


class TestGetActiveSkillView:
    async def test_returns_none_when_no_release(self, svc: SkillLifecycleService):
        result = await svc.get_active_skill_view(
            owner="default", skill_key="nonexistent-skill"
        )
        assert result is None

    async def test_returns_view_with_active_release(self, svc: SkillLifecycleService):
        candidate, release = await _make_candidate_with_release(
            svc,
            skill_key="view-skill",
            summary="Loads CSV data",
            usage_notes="Requires valid credentials",
        )

        view = await svc.get_active_skill_view(owner="default", skill_key="view-skill")

        assert view is not None
        assert view["skill_key"] == "view-skill"
        assert view["release_id"] == release.id
        assert view["version"] == release.version
        assert view["stage"] == "canary"
        assert view["summary"] == "Loads CSV data"
        assert view["goal"] is None  # not declared yet

    async def test_includes_goal_when_declared(self, svc: SkillLifecycleService):
        await _make_candidate_with_release(svc, skill_key="goal-skill")
        await svc.declare_goal(
            owner="default",
            skill_key="goal-skill",
            goal="Load CSV and validate schema.",
        )

        view = await svc.get_active_skill_view(owner="default", skill_key="goal-skill")

        assert view is not None
        assert view["goal"] == "Load CSV and validate schema."

    async def test_content_contains_skill_key(self, svc: SkillLifecycleService):
        await _make_candidate_with_release(svc, skill_key="content-skill")

        view = await svc.get_active_skill_view(owner="default", skill_key="content-skill")

        assert view is not None
        assert "content-skill" in view["content"]

    async def test_content_assembles_summary_and_usage_notes(self, svc: SkillLifecycleService):
        await _make_candidate_with_release(
            svc,
            skill_key="rich-skill",
            summary="Opens GitHub repo page",
            usage_notes="Requires sandbox with browser capability",
        )

        view = await svc.get_active_skill_view(owner="default", skill_key="rich-skill")

        assert view is not None
        assert "Opens GitHub repo page" in view["content"]
        assert "Requires sandbox with browser capability" in view["content"]

    async def test_content_assembles_preconditions_and_postconditions(
        self, svc: SkillLifecycleService
    ):
        await _make_candidate_with_release(
            svc,
            skill_key="cond-skill",
            summary="Star counter",
            preconditions={"browser": "available"},
            postconditions={"result": "integer"},
        )

        view = await svc.get_active_skill_view(owner="default", skill_key="cond-skill")

        assert view is not None
        assert "browser" in view["content"]
        assert "result" in view["content"]

    async def test_returns_payload_ref_from_candidate(self, svc: SkillLifecycleService):
        candidate, release = await _make_candidate_with_release(
            svc, skill_key="ref-skill"
        )

        view = await svc.get_active_skill_view(owner="default", skill_key="ref-skill")

        assert view is not None
        assert view["payload_ref"] == candidate.payload_ref

    async def test_owner_isolation(self, svc: SkillLifecycleService):
        await _make_candidate_with_release(
            svc, owner="alice", skill_key="iso-skill", summary="Alice's skill"
        )

        view = await svc.get_active_skill_view(owner="bob", skill_key="iso-skill")
        assert view is None
