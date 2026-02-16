"""Unit tests for browser learning processor/scheduler."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.config import BrowserLearningConfig
from app.models.skill import (
    ExecutionType,
    LearnStatus,
    SkillReleaseMode,
    SkillReleaseStage,
    SkillType,
)
from app.services.skills import scheduler as scheduler_module
from app.services.skills.scheduler import BrowserLearningProcessor
from app.services.skills.service import SkillLifecycleService
from app.utils.datetime import utcnow


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def learning_config() -> BrowserLearningConfig:
    return BrowserLearningConfig(
        enabled=True,
        run_on_startup=False,
        interval_seconds=60,
        batch_size=20,
        score_threshold=0.85,
        replay_success_threshold=0.95,
        min_samples=30,
        canary_window_hours=24,
        success_drop_threshold=0.03,
        error_rate_multiplier_threshold=2.0,
    )


@pytest.fixture
def skill_service(db_session: AsyncSession) -> SkillLifecycleService:
    return SkillLifecycleService(db_session)


@pytest.mark.asyncio
async def test_extract_segments_excludes_failed_and_read_only(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    blob = await skill_service.create_artifact_blob(
        owner="default",
        kind="browser_trace",
        payload={
            "steps": [
                {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0},
                {"kind": "individual_action", "cmd": "click @e1", "exit_code": 0},
                {"kind": "individual_action", "cmd": "snapshot -i", "exit_code": 0},
                {"kind": "individual_action", "cmd": "fill @e2 hello", "exit_code": 1},
                {"kind": "individual_action", "cmd": "type @e2 world", "exit_code": 0},
            ]
        },
    )
    entry = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="batch",
        success=False,
        execution_time_ms=22,
        payload_ref=skill_service.make_blob_ref(blob.id),
        learn_enabled=True,
        learn_status=LearnStatus.PENDING,
    )
    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=True,
    )

    segments = await processor._extract_segments(entry=entry)
    assert len(segments) == 1
    assert [step["cmd"] for step in segments[0]] == [
        "open https://example.com",
        "click @e1",
    ]


@pytest.mark.asyncio
async def test_processor_promotes_canary_when_threshold_pass(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    blob = await skill_service.create_artifact_blob(
        owner="default",
        kind="browser_trace",
        payload={
            "steps": [
                {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0},
                {"kind": "individual_action", "cmd": "click @e1", "exit_code": 0},
                {"kind": "individual_action", "cmd": "fill @e2 hello", "exit_code": 0},
            ]
        },
    )
    entry = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="batch",
        success=True,
        execution_time_ms=20,
        payload_ref=skill_service.make_blob_ref(blob.id),
        tags="skill:browser-login",
        learn_enabled=True,
        learn_status=LearnStatus.PENDING,
    )
    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=True,
    )
    cycle = await processor.run_cycle()
    assert cycle.processed_executions == 1
    assert cycle.promoted_canary == 1

    candidate_items, candidate_total = await skill_service.list_candidates(owner="default")
    assert candidate_total == 1
    assert candidate_items[0].skill_type == SkillType.BROWSER

    releases, total = await skill_service.list_releases(owner="default", active_only=True)
    assert total == 1
    assert releases[0].stage == SkillReleaseStage.CANARY
    assert releases[0].release_mode == SkillReleaseMode.AUTO

    refreshed = await skill_service.get_execution_by_id(owner="default", execution_id=entry.id)
    assert refreshed.learn_status == LearnStatus.PROCESSED


@pytest.mark.asyncio
async def test_processor_does_not_promote_when_kill_switch_disabled(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    blob = await skill_service.create_artifact_blob(
        owner="default",
        kind="browser_trace",
        payload={
            "steps": [
                {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0},
                {"kind": "individual_action", "cmd": "click @e1", "exit_code": 0},
                {"kind": "individual_action", "cmd": "fill @e2 hello", "exit_code": 0},
            ]
        },
    )
    await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="batch",
        success=True,
        execution_time_ms=20,
        payload_ref=skill_service.make_blob_ref(blob.id),
        tags="skill:browser-login",
        learn_enabled=True,
        learn_status=LearnStatus.PENDING,
    )
    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=False,
    )
    cycle = await processor.run_cycle()
    assert cycle.promoted_canary == 0

    candidates, total = await skill_service.list_candidates(owner="default")
    assert total == 1
    assert candidates[0].auto_release_reason == "auto_release_disabled"
    releases, release_total = await skill_service.list_releases(owner="default")
    assert release_total == 0


@pytest.mark.asyncio
async def test_auto_promotes_stable_after_healthy_window(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    base_exec = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="baseline",
        success=True,
        execution_time_ms=12,
    )
    candidate = await skill_service.create_candidate(
        owner="default",
        skill_key="browser-search",
        source_execution_ids=[base_exec.id],
        skill_type=SkillType.BROWSER,
    )
    await skill_service.evaluate_candidate(
        owner="default",
        candidate_id=candidate.id,
        passed=True,
        score=0.96,
    )
    canary_release = await skill_service.promote_candidate(
        owner="default",
        candidate_id=candidate.id,
        stage=SkillReleaseStage.CANARY,
        promoted_by="system:auto",
        release_mode=SkillReleaseMode.AUTO,
        health_window_end_at=utcnow() + timedelta(seconds=1),
    )
    await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="canary replay",
        success=True,
        execution_time_ms=10,
        tags=f"release:{canary_release.id},skill:browser-search",
    )
    await asyncio.sleep(1.1)

    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=True,
    )
    cycle = await processor.run_cycle()
    assert cycle.promoted_stable == 1

    releases, _total = await skill_service.list_releases(
        owner="default",
        skill_key="browser-search",
        active_only=True,
    )
    assert len(releases) == 1
    assert releases[0].stage == SkillReleaseStage.STABLE
    assert releases[0].release_mode == SkillReleaseMode.AUTO


@pytest.mark.asyncio
async def test_auto_rollback_on_error_regression(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    stable_exec = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="stable",
        success=True,
        execution_time_ms=10,
    )
    stable_candidate = await skill_service.create_candidate(
        owner="default",
        skill_key="browser-checkout",
        source_execution_ids=[stable_exec.id],
        skill_type=SkillType.BROWSER,
    )
    await skill_service.evaluate_candidate(
        owner="default",
        candidate_id=stable_candidate.id,
        passed=True,
        score=0.98,
    )
    stable_release = await skill_service.promote_candidate(
        owner="default",
        candidate_id=stable_candidate.id,
        stage=SkillReleaseStage.STABLE,
    )
    for _ in range(4):
        await skill_service.create_execution(
            owner="default",
            sandbox_id="sandbox-1",
            exec_type=ExecutionType.BROWSER_BATCH,
            code="stable replay",
            success=True,
            execution_time_ms=8,
            tags=f"release:{stable_release.id},skill:browser-checkout",
        )

    canary_exec = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="canary",
        success=True,
        execution_time_ms=10,
    )
    canary_candidate = await skill_service.create_candidate(
        owner="default",
        skill_key="browser-checkout",
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
        tags=f"release:{canary_release.id},skill:browser-checkout",
    )

    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=True,
    )
    cycle = await processor.run_cycle()
    assert cycle.rolled_back == 1


@pytest.mark.asyncio
async def test_processor_skips_when_segments_are_shorter_than_two_steps(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    blob = await skill_service.create_artifact_blob(
        owner="default",
        kind="browser_trace",
        payload={
            "steps": [
                {"kind": "individual_action", "cmd": "open https://example.com", "exit_code": 0}
            ]
        },
    )
    entry = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER,
        code="open https://example.com",
        success=True,
        execution_time_ms=9,
        payload_ref=skill_service.make_blob_ref(blob.id),
        learn_enabled=True,
        learn_status=LearnStatus.PENDING,
    )

    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=True,
    )
    cycle = await processor.run_cycle()

    assert cycle.processed_executions == 0
    assert cycle.skipped_executions == 1
    refreshed = await skill_service.get_execution_by_id(owner="default", execution_id=entry.id)
    assert refreshed.learn_status == LearnStatus.SKIPPED
    assert refreshed.learn_error == "no_actionable_segments"


@pytest.mark.asyncio
async def test_processor_marks_execution_error_when_trace_ref_is_invalid(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    entry = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="open https://example.com\nclick @e1",
        success=True,
        execution_time_ms=12,
        payload_ref="blob:does-not-exist",
        learn_enabled=True,
        learn_status=LearnStatus.PENDING,
    )

    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=True,
    )
    cycle = await processor.run_cycle()

    assert cycle.errored_executions == 1
    refreshed = await skill_service.get_execution_by_id(owner="default", execution_id=entry.id)
    assert refreshed.learn_status == LearnStatus.ERROR
    assert refreshed.learn_error is not None
    assert "Artifact blob not found" in refreshed.learn_error


@pytest.mark.asyncio
async def test_auto_stable_promotion_blocked_when_auto_release_disabled(
    skill_service: SkillLifecycleService,
    learning_config: BrowserLearningConfig,
):
    base_exec = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="baseline",
        success=True,
        execution_time_ms=10,
    )
    candidate = await skill_service.create_candidate(
        owner="default",
        skill_key="browser-disabled-stable",
        source_execution_ids=[base_exec.id],
        skill_type=SkillType.BROWSER,
    )
    await skill_service.evaluate_candidate(
        owner="default",
        candidate_id=candidate.id,
        passed=True,
        score=0.97,
        report='{"replay_success":0.99,"samples":80}',
    )
    canary_release = await skill_service.promote_candidate(
        owner="default",
        candidate_id=candidate.id,
        stage=SkillReleaseStage.CANARY,
        promoted_by="system:auto",
        release_mode=SkillReleaseMode.AUTO,
        health_window_end_at=utcnow() - timedelta(seconds=1),
    )
    await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-1",
        exec_type=ExecutionType.BROWSER_BATCH,
        code="healthy canary replay",
        success=True,
        execution_time_ms=8,
        tags=f"release:{canary_release.id},skill:browser-disabled-stable",
    )

    processor = BrowserLearningProcessor(
        service=skill_service,
        config=learning_config,
        auto_release_enabled=False,
    )
    cycle = await processor.run_cycle()

    assert cycle.promoted_stable == 0
    active_release = await skill_service.get_active_release(
        owner="default",
        skill_key="browser-disabled-stable",
    )
    assert active_release is not None
    assert active_release.id == canary_release.id
    assert active_release.stage == SkillReleaseStage.CANARY


@pytest.mark.asyncio
async def test_derive_skill_and_scenario_keys_from_tags_and_description(
    skill_service: SkillLifecycleService,
):
    entry = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-k",
        exec_type=ExecutionType.BROWSER,
        code="open https://example.com",
        success=True,
        execution_time_ms=7,
        tags="foo,skill:browser-checkout,bar",
        description=" Checkout Flow / Happy Path #1 ",
    )

    derived_skill_key = BrowserLearningProcessor._derive_skill_key(entry=entry)
    derived_scenario = BrowserLearningProcessor._derive_scenario_key(entry=entry)

    assert derived_skill_key == "browser-checkout"
    assert derived_scenario == "checkout-flow-happy-path-1"

    entry_without_skill = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-fallback",
        exec_type=ExecutionType.BROWSER,
        code="open https://example.com",
        success=True,
        execution_time_ms=6,
        tags="foo,bar",
        description="   ",
    )
    fallback_skill_key = BrowserLearningProcessor._derive_skill_key(entry=entry_without_skill)
    assert fallback_skill_key == "browser-sandbox-fallback"
    assert BrowserLearningProcessor._derive_scenario_key(entry=entry_without_skill) is None


@pytest.mark.asyncio
async def test_normalize_steps_falls_back_to_execution_code_when_trace_missing(
    skill_service: SkillLifecycleService,
):
    entry = await skill_service.create_execution(
        owner="default",
        sandbox_id="sandbox-fallback",
        exec_type=ExecutionType.BROWSER,
        code="open about:blank",
        success=False,
        execution_time_ms=5,
        learn_enabled=True,
        learn_status=LearnStatus.PENDING,
    )

    normalized = BrowserLearningProcessor._normalize_steps(
        entry=entry,
        trace_payload=None,
    )
    assert normalized == [
        {
            "cmd": "open about:blank",
            "exit_code": 1,
            "kind": "individual_action",
        }
    ]


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("snapshot -i", True),
        ("get text @e1", True),
        ("wait --load networkidle", True),
        ("click @e1", False),
        ("fill @e2 hello", False),
    ],
)
def test_is_read_only_command_prefixes(cmd: str, expected: bool):
    assert BrowserLearningProcessor._is_read_only_command(cmd) is expected


@pytest.mark.asyncio
async def test_scheduler_run_once_returns_empty_result_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    learning_config: BrowserLearningConfig,
):
    scheduler = scheduler_module.BrowserLearningScheduler(config=learning_config)
    settings = SimpleNamespace(
        browser_learning=learning_config.model_copy(update={"enabled": False}),
        browser_auto_release_enabled=True,
    )
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: settings)

    cycle = await scheduler.run_once()
    assert cycle.processed_executions == 0
    assert cycle.skipped_executions == 0
    assert cycle.errored_executions == 0
    assert cycle.created_candidates == 0
    assert cycle.promoted_canary == 0
    assert cycle.promoted_stable == 0
    assert cycle.rolled_back == 0
    assert cycle.audit_events == []


@pytest.mark.asyncio
async def test_scheduler_start_stop_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    learning_config: BrowserLearningConfig,
):
    scheduler = scheduler_module.BrowserLearningScheduler(config=learning_config)

    async def fake_background_loop() -> None:
        while True:
            await asyncio.sleep(1)

    monkeypatch.setattr(scheduler, "_background_loop", fake_background_loop)

    await scheduler.start()
    assert scheduler.is_running is True
    first_task = scheduler._task
    assert first_task is not None

    await scheduler.start()
    assert scheduler._task is first_task

    await scheduler.stop()
    assert scheduler.is_running is False
    assert scheduler._task is None

    # stop again should be noop
    await scheduler.stop()
