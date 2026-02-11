"""Unit tests for SkillLifecycleService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.skill import (
    ExecutionType,
    LearnStatus,
    SkillCandidateStatus,
    SkillReleaseMode,
    SkillReleaseStage,
    SkillType,
)
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

    async def test_list_pending_browser_learning_filters_by_type_status_and_flag(
        self,
        skill_service: SkillLifecycleService,
    ):
        pending_browser = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER,
            code="open https://example.com",
            success=True,
            execution_time_ms=5,
            learn_enabled=True,
            learn_status=LearnStatus.PENDING,
        )
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="open https://example.com\nsnapshot -i",
            success=True,
            execution_time_ms=7,
            learn_enabled=True,
            learn_status=LearnStatus.PROCESSING,
        )
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.PYTHON,
            code="print('x')",
            success=True,
            execution_time_ms=3,
            learn_enabled=True,
            learn_status=LearnStatus.PENDING,
        )
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER,
            code="snapshot -i",
            success=True,
            execution_time_ms=4,
            learn_enabled=False,
        )

        pending = await skill_service.list_pending_browser_learning_executions(limit=20)
        assert [item.id for item in pending] == [pending_browser.id]

    async def test_list_pending_browser_learning_validates_limit(
        self,
        skill_service: SkillLifecycleService,
    ):
        with pytest.raises(ValidationError, match="limit must be between 1 and 500"):
            await skill_service.list_pending_browser_learning_executions(limit=0)
        with pytest.raises(ValidationError, match="limit must be between 1 and 500"):
            await skill_service.list_pending_browser_learning_executions(limit=501)


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


class TestBrowserSkillExtensions:
    async def test_execution_learning_and_blob_payload_round_trip(
        self,
        skill_service: SkillLifecycleService,
    ):
        blob = await skill_service.create_artifact_blob(
            owner="default",
            kind="browser_trace",
            payload={
                "kind": "browser_batch_trace",
                "steps": [
                    {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0}
                ],
            },
        )
        payload_ref = skill_service.make_blob_ref(blob.id)
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER,
            code="open https://example.com",
            success=True,
            execution_time_ms=10,
            payload_ref=payload_ref,
            learn_enabled=True,
            learn_status=LearnStatus.PENDING,
        )
        assert entry.payload_ref == payload_ref
        assert entry.learn_enabled is True
        assert entry.learn_status == LearnStatus.PENDING

        payload = await skill_service.get_payload_by_ref(owner="default", payload_ref=payload_ref)
        assert isinstance(payload, dict)
        assert payload["steps"][0]["cmd"] == "open https://example.com"

    async def test_get_payload_with_blob_by_ref_returns_blob_kind_and_payload(
        self,
        skill_service: SkillLifecycleService,
    ):
        blob = await skill_service.create_artifact_blob(
            owner="default",
            kind="candidate_payload",
            payload={"commands": ["open about:blank"]},
        )
        payload_ref = skill_service.make_blob_ref(blob.id)

        resolved_blob, payload = await skill_service.get_payload_with_blob_by_ref(
            owner="default",
            payload_ref=payload_ref,
        )

        assert resolved_blob.id == blob.id
        assert resolved_blob.kind == "candidate_payload"
        assert isinstance(payload, dict)
        assert payload["commands"] == ["open about:blank"]

    async def test_payload_ref_validation_and_json_decode_error(
        self,
        skill_service: SkillLifecycleService,
    ):
        with pytest.raises(ValidationError, match="Unsupported payload_ref"):
            await skill_service.get_payload_by_ref(owner="default", payload_ref="s3://blob-1")

        with pytest.raises(ValidationError, match="Invalid payload_ref"):
            await skill_service.get_payload_by_ref(owner="default", payload_ref="blob:")

        blob = await skill_service.create_artifact_blob(
            owner="default",
            kind="browser_trace",
            payload={"steps": []},
        )
        blob.payload_json = "{bad-json"
        await skill_service._db.commit()  # test-only state injection

        with pytest.raises(ValidationError, match="Invalid payload JSON in blob"):
            await skill_service.get_payload_by_ref(
                owner="default",
                payload_ref=skill_service.make_blob_ref(blob.id),
            )

    async def test_merge_tags_deduplicates_and_sorts(self):
        merged = SkillLifecycleService.merge_tags(
            " skill:checkout , release:sr-2 ",
            "release:sr-2,stage:canary",
            None,
            "skill:checkout",
        )
        assert merged == "release:sr-2,skill:checkout,stage:canary"

        assert SkillLifecycleService.merge_tags(None, "  ,  ") is None

    async def test_browser_candidate_auto_release_fields(
        self,
        skill_service: SkillLifecycleService,
    ):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="open https://example.com\nclick @e1",
            success=True,
            execution_time_ms=21,
        )
        candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-login",
            source_execution_ids=[entry.id],
            skill_type=SkillType.BROWSER,
            auto_release_eligible=False,
            auto_release_reason="pending",
            created_by="system:auto",
        )
        assert candidate.skill_type == SkillType.BROWSER
        assert candidate.auto_release_eligible is False
        assert candidate.auto_release_reason == "pending"

        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate.id,
            passed=True,
            score=0.96,
            report='{"replay_success":0.98,"samples":40}',
        )
        release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate.id,
            stage=SkillReleaseStage.CANARY,
            promoted_by="system:auto",
            release_mode=SkillReleaseMode.AUTO,
        )
        refreshed = await skill_service.get_candidate(owner="default", candidate_id=candidate.id)
        assert release.release_mode == SkillReleaseMode.AUTO
        assert refreshed.status == SkillCandidateStatus.PROMOTED_CANARY

    async def test_auto_stable_promotion_sets_browser_candidate_status(
        self,
        skill_service: SkillLifecycleService,
    ):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="open https://example.com\nclick @e1",
            success=True,
            execution_time_ms=14,
        )
        candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-stable",
            source_execution_ids=[entry.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate.id,
            passed=True,
            score=0.99,
            report='{"replay_success":0.99,"samples":80}',
        )
        release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate.id,
            stage=SkillReleaseStage.STABLE,
            promoted_by="system:auto",
            release_mode=SkillReleaseMode.AUTO,
        )
        refreshed = await skill_service.get_candidate(owner="default", candidate_id=candidate.id)
        assert release.stage == SkillReleaseStage.STABLE
        assert release.release_mode == SkillReleaseMode.AUTO
        assert refreshed.status == SkillCandidateStatus.PROMOTED_STABLE

    async def test_release_health_detects_success_drop_for_rollback(
        self,
        skill_service: SkillLifecycleService,
    ):
        base_exec = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="open https://example.com\nclick @e1\nfill @e2 foo",
            success=True,
            execution_time_ms=10,
        )
        base_candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-search",
            source_execution_ids=[base_exec.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=base_candidate.id,
            passed=True,
            score=0.98,
            report='{"replay_success":0.99,"samples":100}',
        )
        base_release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=base_candidate.id,
            stage=SkillReleaseStage.STABLE,
            promoted_by="default",
        )
        # baseline executions (all successful)
        for _ in range(5):
            await skill_service.create_execution(
                owner="default",
                sandbox_id="sandbox-1",
                exec_type=ExecutionType.BROWSER_BATCH,
                code="replay stable",
                success=True,
                execution_time_ms=8,
                tags=f"release:{base_release.id},skill:browser-search",
            )

        canary_exec = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="open https://example.com\nclick @e1\nfill @e2 foo",
            success=True,
            execution_time_ms=9,
        )
        canary_candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-search",
            source_execution_ids=[canary_exec.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=canary_candidate.id,
            passed=True,
            score=0.91,
            report='{"replay_success":0.96,"samples":60}',
        )
        canary_release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=canary_candidate.id,
            stage=SkillReleaseStage.CANARY,
            promoted_by="system:auto",
            release_mode=SkillReleaseMode.AUTO,
        )

        # degraded canary signal: 2/5 failures -> success rate 60%
        outcomes = [True, False, False, True, True]
        for outcome in outcomes:
            await skill_service.create_execution(
                owner="default",
                sandbox_id="sandbox-1",
                exec_type=ExecutionType.BROWSER_BATCH,
                code="replay canary",
                success=outcome,
                execution_time_ms=11,
                tags=f"release:{canary_release.id},skill:browser-search",
            )

        health = await skill_service.get_release_health(
            owner="default",
            release_id=canary_release.id,
        )
        assert health["should_rollback"] is True
        assert "success_rate_drop" in health["rollback_reasons"]

    async def test_release_health_uses_previous_evaluation_as_baseline_fallback(
        self,
        skill_service: SkillLifecycleService,
    ):
        prev_exec = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="baseline source",
            success=True,
            execution_time_ms=12,
        )
        prev_candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-baseline-fallback",
            source_execution_ids=[prev_exec.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=prev_candidate.id,
            passed=True,
            score=0.9,
            report='{"replay_success":0.91,"error_rate":0.09,"samples":55,"p95_duration":2000}',
        )
        await skill_service.promote_candidate(
            owner="default",
            candidate_id=prev_candidate.id,
            stage=SkillReleaseStage.STABLE,
        )

        canary_exec = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="canary source",
            success=True,
            execution_time_ms=10,
        )
        canary_candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-baseline-fallback",
            source_execution_ids=[canary_exec.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=canary_candidate.id,
            passed=True,
            score=0.95,
            report='{"replay_success":0.97,"samples":60}',
        )
        canary_release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=canary_candidate.id,
            stage=SkillReleaseStage.CANARY,
            promoted_by="system:auto",
            release_mode=SkillReleaseMode.AUTO,
        )
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="canary replay",
            success=True,
            execution_time_ms=11,
            tags=f"release:{canary_release.id},skill:browser-baseline-fallback",
        )

        health = await skill_service.get_release_health(
            owner="default",
            release_id=canary_release.id,
        )
        assert health["baseline_samples"] == 55
        assert health["baseline_success_rate"] == pytest.approx(0.91)
        assert health["baseline_error_rate"] == pytest.approx(0.09)
        assert health["should_rollback"] is False

    async def test_release_health_without_observations_does_not_trigger_rollback(
        self,
        skill_service: SkillLifecycleService,
    ):
        entry = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="source",
            success=True,
            execution_time_ms=8,
        )
        candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-no-observed-samples",
            source_execution_ids=[entry.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=candidate.id,
            passed=True,
            score=0.92,
        )
        release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=candidate.id,
            stage=SkillReleaseStage.CANARY,
            promoted_by="system:auto",
            release_mode=SkillReleaseMode.AUTO,
        )

        health = await skill_service.get_release_health(owner="default", release_id=release.id)
        assert health["samples"] == 0
        assert health["healthy"] is False
        assert health["should_rollback"] is False

    async def test_release_health_rolls_back_when_baseline_error_rate_is_zero_and_canary_fails(
        self,
        skill_service: SkillLifecycleService,
    ):
        stable_exec = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="stable source",
            success=True,
            execution_time_ms=9,
        )
        stable_candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-zero-error-baseline",
            source_execution_ids=[stable_exec.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=stable_candidate.id,
            passed=True,
            score=0.97,
            report='{"replay_success":1.0,"error_rate":0.0,"samples":100}',
        )
        await skill_service.promote_candidate(
            owner="default",
            candidate_id=stable_candidate.id,
            stage=SkillReleaseStage.STABLE,
        )

        canary_exec = await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="canary source",
            success=True,
            execution_time_ms=10,
        )
        canary_candidate = await skill_service.create_candidate(
            owner="default",
            skill_key="browser-zero-error-baseline",
            source_execution_ids=[canary_exec.id],
            skill_type=SkillType.BROWSER,
        )
        await skill_service.evaluate_candidate(
            owner="default",
            candidate_id=canary_candidate.id,
            passed=True,
            score=0.9,
        )
        canary_release = await skill_service.promote_candidate(
            owner="default",
            candidate_id=canary_candidate.id,
            stage=SkillReleaseStage.CANARY,
            promoted_by="system:auto",
            release_mode=SkillReleaseMode.AUTO,
        )
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="canary replay",
            success=False,
            execution_time_ms=11,
            tags=f"release:{canary_release.id},skill:browser-zero-error-baseline",
        )

        health = await skill_service.get_release_health(
            owner="default",
            release_id=canary_release.id,
        )
        assert health["baseline_error_rate"] == 0.0
        assert health["error_rate_multiplier"] > 1000
        assert health["should_rollback"] is True
        assert "error_rate_regression" in health["rollback_reasons"]
