"""Unit tests for SkillLifecycleService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.skill import ExecutionType, SkillCandidateStatus, SkillReleaseStage
from app.services.skills import SkillLifecycleService


@pytest.fixture
async def db_session() -> AsyncSession:
    """Create in-memory SQLite database/session for unit tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async_session_factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def skill_service(db_session: AsyncSession) -> SkillLifecycleService:
    """SkillLifecycleService instance."""
    return SkillLifecycleService(db_session)


class TestExecutionHistory:
    async def test_create_list_and_annotate_execution(self, skill_service: SkillLifecycleService):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            session_id="sess-1",
            exec_type=ExecutionType.PYTHON,
            code="print('hello')",
            success=True,
            execution_time_ms=8,
            output="hello\n",
            description="initial run",
            tags="demo,python",
        )

        assert entry.id.startswith("exec-")

        entries, total = await skill_service.list_execution_history(
            owner="default",
            sandbox_id="sandbox-1",
            success_only=True,
            tags="demo",
            limit=10,
            offset=0,
        )
        assert total == 1
        assert entries[0].id == entry.id

        updated = await skill_service.annotate_execution(
            owner="default",
            sandbox_id="sandbox-1",
            execution_id=entry.id,
            notes="reusable snippet",
        )
        assert updated.notes == "reusable snippet"

    async def test_list_history_filters_and_tag_normalization(
        self, skill_service: SkillLifecycleService
    ):
        entry_a = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('a')",
            success=True,
            execution_time_ms=3,
            description="desc-a",
            tags=" alpha,beta,alpha ",
        )
        entry_b = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.SHELL,
            code="echo b",
            success=False,
            execution_time_ms=4,
            tags="ops",
        )
        await skill_service.annotate_execution(
            owner="default",
            sandbox_id="sandbox-1",
            execution_id=entry_b.id,
            notes="shell note",
        )
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="raise RuntimeError('boom')",
            success=False,
            execution_time_ms=2,
            tags="python,error",
        )

        tagged, tagged_total = await skill_service.list_execution_history(
            owner="default",
            sandbox_id="sandbox-1",
            tags="beta",
            limit=10,
            offset=0,
        )
        assert tagged_total == 1
        assert tagged[0].id == entry_a.id
        assert tagged[0].tags == "alpha,beta"

        described, described_total = await skill_service.list_execution_history(
            owner="default",
            sandbox_id="sandbox-1",
            has_description=True,
            limit=10,
            offset=0,
        )
        assert described_total == 1
        assert described[0].id == entry_a.id

        noted, noted_total = await skill_service.list_execution_history(
            owner="default",
            sandbox_id="sandbox-1",
            has_notes=True,
            limit=10,
            offset=0,
        )
        assert noted_total == 1
        assert noted[0].id == entry_b.id

        successful_python, successful_python_total = await skill_service.list_execution_history(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            success_only=True,
            limit=10,
            offset=0,
        )
        assert successful_python_total == 1
        assert successful_python[0].id == entry_a.id

    async def test_history_validates_limit_and_offset(self, skill_service: SkillLifecycleService):
        with pytest.raises(ValidationError, match="limit must be between 1 and 500"):
            await skill_service.list_execution_history(
                owner="default",
                sandbox_id="sandbox-1",
                limit=0,
                offset=0,
            )

        with pytest.raises(ValidationError, match="offset must be >= 0"):
            await skill_service.list_execution_history(
                owner="default",
                sandbox_id="sandbox-1",
                limit=10,
                offset=-1,
            )

    async def test_get_execution_is_owner_scoped(self, skill_service: SkillLifecycleService):
        entry = await skill_service.create_execution(
            owner="owner-a",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('private')",
            success=True,
            execution_time_ms=3,
        )

        with pytest.raises(NotFoundError, match="Execution not found"):
            await skill_service.get_execution(
                owner="owner-b",
                sandbox_id="sandbox-1",
                execution_id=entry.id,
            )

    async def test_get_last_execution_filters_by_type(self, skill_service: SkillLifecycleService):
        shell_entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.SHELL,
            code="echo hi",
            success=True,
            execution_time_ms=1,
        )
        python_entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('hi')",
            success=True,
            execution_time_ms=2,
        )

        latest_any = await skill_service.get_last_execution(owner="default", sandbox_id="sandbox-1")
        latest_shell = await skill_service.get_last_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.SHELL,
        )

        assert latest_any.id == python_entry.id
        assert latest_shell.id == shell_entry.id

        with pytest.raises(NotFoundError, match="No execution history found"):
            await skill_service.get_last_execution(
                owner="default",
                sandbox_id="sandbox-missing",
            )

    async def test_annotate_execution_can_clear_tags(self, skill_service: SkillLifecycleService):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('hello')",
            success=True,
            execution_time_ms=1,
            tags="foo,bar",
        )
        updated = await skill_service.annotate_execution(
            owner="default",
            sandbox_id="sandbox-1",
            execution_id=entry.id,
            tags=" , ",
        )
        assert updated.tags is None


class TestCandidateLifecycle:
    async def test_create_candidate_validates_inputs(self, skill_service: SkillLifecycleService):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.SHELL,
            code="echo ok",
            success=True,
            execution_time_ms=1,
        )

        with pytest.raises(ValidationError, match="skill_key must not be empty"):
            await skill_service.create_candidate(
                owner="default",
                skill_key="  ",
                source_execution_ids=[entry.id],
            )

        with pytest.raises(ValidationError, match="source_execution_ids must not be empty"):
            await skill_service.create_candidate(
                owner="default",
                skill_key="loader",
                source_execution_ids=[],
            )

        with pytest.raises(ValidationError, match="Execution ID not found"):
            await skill_service.create_candidate(
                owner="default",
                skill_key="loader",
                source_execution_ids=["exec-missing"],
            )

    async def test_promote_requires_passing_evaluation(self, skill_service: SkillLifecycleService):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.SHELL,
            code="echo hello",
            success=True,
            execution_time_ms=3,
        )
        candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="echo-skill",
            source_execution_ids=[entry.id],
        )

        with pytest.raises(ConflictError):
            await skill_service.promote_candidate(
                owner="default",
                candidate_id=candidate.id,
                stage=SkillReleaseStage.CANARY,
            )

    async def test_evaluate_failure_marks_candidate_rejected(
        self,
        skill_service: SkillLifecycleService,
    ):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('candidate')",
            success=True,
            execution_time_ms=1,
        )
        candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="candidate-x",
            source_execution_ids=[entry.id],
        )

        updated_candidate, evaluation = await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate.id,
            passed=False,
            score=0.2,
            benchmark_id="bench-fail",
            report="failed checks",
            evaluated_by="qa",
        )

        assert evaluation.passed is False
        assert updated_candidate.status == SkillCandidateStatus.REJECTED
        assert updated_candidate.latest_pass is False
        assert updated_candidate.latest_score == 0.2

    async def test_list_candidates_filters(self, skill_service: SkillLifecycleService):
        entry_a = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('a')",
            success=True,
            execution_time_ms=1,
        )
        entry_b = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('b')",
            success=True,
            execution_time_ms=1,
        )
        candidate_a = await skill_service.create_candidate(
            owner="default",
            skill_key="skill-a",
            source_execution_ids=[entry_a.id],
        )
        candidate_b = await skill_service.create_candidate(
            owner="default",
            skill_key="skill-b",
            source_execution_ids=[entry_b.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_b.id,
            passed=False,
            score=0.1,
        )

        by_key, total_by_key = await skill_service.list_candidates(
            owner="default",
            skill_key="skill-a",
            limit=10,
            offset=0,
        )
        assert total_by_key == 1
        assert by_key[0].id == candidate_a.id

        rejected, rejected_total = await skill_service.list_candidates(
            owner="default",
            status=SkillCandidateStatus.REJECTED,
            limit=10,
            offset=0,
        )
        assert rejected_total == 1
        assert rejected[0].id == candidate_b.id

        page, page_total = await skill_service.list_candidates(
            owner="default",
            limit=1,
            offset=0,
        )
        assert page_total >= 2
        assert len(page) == 1

    async def test_list_candidates_validates_pagination(self, skill_service: SkillLifecycleService):
        with pytest.raises(ValidationError, match="limit must be between 1 and 500"):
            await skill_service.list_candidates(
                owner="default",
                limit=0,
                offset=0,
            )
        with pytest.raises(ValidationError, match="offset must be >= 0"):
            await skill_service.list_candidates(
                owner="default",
                limit=10,
                offset=-1,
            )

    async def test_promote_deactivates_previous_release(self, skill_service: SkillLifecycleService):
        entry_v1 = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('v1')",
            success=True,
            execution_time_ms=1,
        )
        candidate_v1 = await skill_service.create_candidate(
            owner="default",
            skill_key="loader",
            source_execution_ids=[entry_v1.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_v1.id,
            passed=True,
            score=0.8,
        )
        release_v1 = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate_v1.id,
            stage=SkillReleaseStage.CANARY,
            promoted_by="promoter",
        )

        entry_v2 = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('v2')",
            success=True,
            execution_time_ms=1,
        )
        candidate_v2 = await skill_service.create_candidate(
            owner="default",
            skill_key="loader",
            source_execution_ids=[entry_v2.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_v2.id,
            passed=True,
            score=0.95,
        )
        release_v2 = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate_v2.id,
            stage=SkillReleaseStage.STABLE,
            promoted_by="promoter",
        )

        all_releases, total = await skill_service.list_releases(owner="default", skill_key="loader")
        assert total == 2
        release_map = {item.id: item for item in all_releases}
        assert release_map[release_v1.id].is_active is False
        assert release_map[release_v2.id].is_active is True

        refreshed_candidate_v2 = await skill_service.get_candidate(
            owner="default",
            candidate_id=candidate_v2.id,
        )
        assert refreshed_candidate_v2.status == SkillCandidateStatus.PROMOTED
        assert refreshed_candidate_v2.promotion_release_id == release_v2.id

    async def test_list_releases_filters_and_validation(self, skill_service: SkillLifecycleService):
        entry_a = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('stable')",
            success=True,
            execution_time_ms=1,
        )
        candidate_a = await skill_service.create_candidate(
            owner="default",
            skill_key="skill-stable",
            source_execution_ids=[entry_a.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_a.id,
            passed=True,
        )
        await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate_a.id,
            stage=SkillReleaseStage.STABLE,
        )

        entry_b = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.SHELL,
            code="echo canary",
            success=True,
            execution_time_ms=1,
        )
        candidate_b = await skill_service.create_candidate(
            owner="default",
            skill_key="skill-canary",
            source_execution_ids=[entry_b.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_b.id,
            passed=True,
        )
        release_canary = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate_b.id,
            stage=SkillReleaseStage.CANARY,
        )

        stable_releases, stable_total = await skill_service.list_releases(
            owner="default",
            stage=SkillReleaseStage.STABLE,
            limit=10,
            offset=0,
        )
        assert stable_total == 1
        assert stable_releases[0].stage == SkillReleaseStage.STABLE

        active_releases, active_total = await skill_service.list_releases(
            owner="default",
            skill_key="skill-canary",
            active_only=True,
            limit=10,
            offset=0,
        )
        assert active_total == 1
        assert active_releases[0].id == release_canary.id

        with pytest.raises(ValidationError, match="limit must be between 1 and 500"):
            await skill_service.list_releases(
                owner="default",
                limit=0,
                offset=0,
            )
        with pytest.raises(ValidationError, match="offset must be >= 0"):
            await skill_service.list_releases(
                owner="default",
                limit=10,
                offset=-1,
            )

    async def test_rollback_requires_previous_release(self, skill_service: SkillLifecycleService):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('only')",
            success=True,
            execution_time_ms=1,
        )
        candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="one-release-skill",
            source_execution_ids=[entry.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate.id,
            passed=True,
        )
        only_release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate.id,
        )

        with pytest.raises(ConflictError, match="no previous release exists"):
            await skill_service.rollback_release(
                owner="default",
                release_id=only_release.id,
            )

    async def test_evaluate_promote_and_rollback(self, skill_service: SkillLifecycleService):
        entry_a = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('v1')",
            success=True,
            execution_time_ms=2,
        )
        candidate_a = await skill_service.create_candidate(
            owner="default",
            skill_key="loader",
            source_execution_ids=[entry_a.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_a.id,
            passed=True,
            score=0.9,
            benchmark_id="bench-1",
        )
        release_a = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate_a.id,
            stage=SkillReleaseStage.STABLE,
        )

        entry_b = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('v2')",
            success=True,
            execution_time_ms=2,
        )
        candidate_b = await skill_service.create_candidate(
            owner="default",
            skill_key="loader",
            source_execution_ids=[entry_b.id],
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate_b.id,
            passed=True,
            score=0.95,
            benchmark_id="bench-2",
        )
        release_b = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate_b.id,
            stage=SkillReleaseStage.CANARY,
        )

        assert release_b.version == release_a.version + 1

        rollback_release = await skill_service.rollback_release(
            owner="default",
            release_id=release_b.id,
            rolled_back_by="default",
        )
        candidate_b_after = await skill_service.get_candidate(
            owner="default",
            candidate_id=candidate_b.id,
        )

        assert rollback_release.rollback_of == release_b.id
        assert rollback_release.is_active is True
        assert rollback_release.version == release_b.version + 1
        assert candidate_b_after.status == SkillCandidateStatus.ROLLED_BACK
