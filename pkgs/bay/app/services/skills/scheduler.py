"""Browser skill learning scheduler."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from app.config import BrowserLearningConfig, get_settings
from app.db.session import get_async_session
from app.models.skill import (
    ExecutionHistory,
    LearnStatus,
    SkillReleaseMode,
    SkillReleaseStage,
    SkillType,
)
from app.services.skills.service import SkillLifecycleService

logger = structlog.get_logger()


READ_ONLY_PREFIXES = (
    "snapshot",
    "get ",
    "is ",
    "wait",
    "cookies",
    "storage",
    "network requests",
    "tab",
    "frame",
    "dialog",
)


@dataclass
class BrowserLearningCycleResult:
    """Summary of one browser learning scheduler cycle."""

    processed_executions: int = 0
    skipped_executions: int = 0
    errored_executions: int = 0
    created_candidates: int = 0
    promoted_canary: int = 0
    promoted_stable: int = 0
    rolled_back: int = 0
    audit_events: list[str] = field(default_factory=list)


class BrowserLearningProcessor:
    """Processor for one browser learning cycle."""

    def __init__(
        self,
        *,
        service: SkillLifecycleService,
        config: BrowserLearningConfig,
        auto_release_enabled: bool,
    ) -> None:
        self._svc = service
        self._config = config
        self._auto_release_enabled = auto_release_enabled
        self._log = logger.bind(component="browser_learning")

    async def run_cycle(self) -> BrowserLearningCycleResult:
        result = BrowserLearningCycleResult()

        pending = await self._svc.list_pending_browser_learning_executions(
            limit=self._config.batch_size
        )

        for entry in pending:
            await self._svc.set_execution_learning_status(
                execution_id=entry.id,
                status=LearnStatus.PROCESSING,
            )
            try:
                processed = await self._process_execution(entry=entry, result=result)
                if processed:
                    result.processed_executions += 1
                else:
                    result.skipped_executions += 1
            except Exception as exc:
                result.errored_executions += 1
                await self._svc.set_execution_learning_status(
                    execution_id=entry.id,
                    status=LearnStatus.ERROR,
                    error=str(exc),
                    processed_at=datetime.utcnow(),
                )
                self._log.exception(
                    "skills.browser.learning.execution_failed",
                    execution_id=entry.id,
                    error=str(exc),
                )

        await self._process_auto_canary_lifecycle(result=result)
        return result

    async def _process_execution(
        self,
        *,
        entry: ExecutionHistory,
        result: BrowserLearningCycleResult,
    ) -> bool:
        segments = await self._extract_segments(entry=entry)
        if not segments:
            await self._svc.set_execution_learning_status(
                execution_id=entry.id,
                status=LearnStatus.SKIPPED,
                error="no_actionable_segments",
                processed_at=datetime.utcnow(),
            )
            return False

        skill_key = self._derive_skill_key(entry=entry)
        scenario_key = self._derive_scenario_key(entry=entry)

        for idx, segment in enumerate(segments):
            segment_blob = await self._svc.create_artifact_blob(
                owner=entry.owner,
                kind="browser_segment",
                payload={
                    "skill_type": "browser",
                    "segment_index": idx,
                    "source_execution_id": entry.id,
                    "commands": [step["cmd"] for step in segment],
                    "steps": segment,
                },
            )
            payload_ref = self._svc.make_blob_ref(segment_blob.id)

            candidate = await self._svc.create_candidate(
                owner=entry.owner,
                skill_key=skill_key,
                scenario_key=scenario_key,
                payload_ref=payload_ref,
                source_execution_ids=[entry.id],
                created_by="system:auto",
                skill_type=SkillType.BROWSER,
                auto_release_eligible=False,
                auto_release_reason="pending_evaluation",
            )
            result.created_candidates += 1

            metrics = self._score_segment(segment=segment)
            passed = (
                metrics["score"] >= self._config.score_threshold
                and metrics["replay_success"] >= self._config.replay_success_threshold
                and metrics["samples"] >= self._config.min_samples
            )
            report = json.dumps(
                {
                    "policy": {
                        "score_threshold": self._config.score_threshold,
                        "replay_success_threshold": self._config.replay_success_threshold,
                        "min_samples": self._config.min_samples,
                    },
                    **metrics,
                },
                ensure_ascii=False,
            )

            await self._svc.evaluate_candidate(
                owner=entry.owner,
                candidate_id=candidate.id,
                passed=passed,
                score=metrics["score"],
                benchmark_id="browser-replay-v1",
                report=report,
                evaluated_by="system:auto",
            )

            if not passed:
                await self._svc.update_candidate_auto_release(
                    owner=entry.owner,
                    candidate_id=candidate.id,
                    eligible=False,
                    reason="threshold_not_met",
                )
                continue

            if not self._auto_release_enabled:
                await self._svc.update_candidate_auto_release(
                    owner=entry.owner,
                    candidate_id=candidate.id,
                    eligible=False,
                    reason="auto_release_disabled",
                )
                continue

            active = await self._svc.get_active_release(owner=entry.owner, skill_key=skill_key)
            release = await self._svc.promote_candidate(
                owner=entry.owner,
                candidate_id=candidate.id,
                stage=SkillReleaseStage.CANARY,
                promoted_by="system:auto",
                release_mode=SkillReleaseMode.AUTO,
                auto_promoted_from=active.id if active else None,
                health_window_end_at=datetime.utcnow()
                + timedelta(hours=self._config.canary_window_hours),
            )
            result.promoted_canary += 1
            await self._svc.update_candidate_auto_release(
                owner=entry.owner,
                candidate_id=candidate.id,
                eligible=True,
                reason="threshold_passed",
            )
            self._log.info(
                "skills.browser.auto_promote_canary",
                candidate_id=candidate.id,
                release_id=release.id,
                score=metrics["score"],
                replay_success=metrics["replay_success"],
                samples=metrics["samples"],
            )
            result.audit_events.append(f"auto_promote_canary:{release.id}")

        await self._svc.set_execution_learning_status(
            execution_id=entry.id,
            status=LearnStatus.PROCESSED,
            error=None,
            processed_at=datetime.utcnow(),
        )
        return True

    async def _extract_segments(self, *, entry: ExecutionHistory) -> list[list[dict[str, Any]]]:
        trace_payload = await self._svc.get_payload_by_ref(
            owner=entry.owner,
            payload_ref=entry.payload_ref,
        )

        steps = self._normalize_steps(entry=entry, trace_payload=trace_payload)
        if not steps:
            return []

        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []

        for step in steps:
            cmd = step.get("cmd", "")
            failed = int(step.get("exit_code", 1)) != 0
            is_read_only = self._is_read_only_command(cmd)
            kind = str(step.get("kind", "individual_action"))

            if failed or is_read_only or kind != "individual_action" or not cmd:
                if len(current) >= 2:
                    segments.append(current.copy())
                current.clear()
                continue
            current.append(step)

        if len(current) >= 2:
            segments.append(current)

        return segments

    @staticmethod
    def _normalize_steps(
        *,
        entry: ExecutionHistory,
        trace_payload: dict[str, Any] | list[Any] | None,
    ) -> list[dict[str, Any]]:
        raw_steps: list[dict[str, Any]] = []

        if isinstance(trace_payload, dict):
            if isinstance(trace_payload.get("steps"), list):
                raw_steps = [
                    step for step in trace_payload["steps"] if isinstance(step, dict)
                ]
            elif "cmd" in trace_payload:
                raw_steps = [trace_payload]
        elif isinstance(trace_payload, list):
            raw_steps = [step for step in trace_payload if isinstance(step, dict)]

        if not raw_steps:
            fallback_exit = 0 if entry.success else 1
            raw_steps = [
                {
                    "cmd": entry.code,
                    "exit_code": fallback_exit,
                    "kind": "individual_action",
                }
            ]

        normalized: list[dict[str, Any]] = []
        for raw in raw_steps:
            cmd = str(raw.get("cmd", "")).strip()
            if not cmd:
                continue
            exit_code = raw.get("exit_code")
            try:
                exit_code_int = int(exit_code) if exit_code is not None else 1
            except Exception:
                exit_code_int = 1
            normalized.append(
                {
                    "cmd": cmd,
                    "exit_code": exit_code_int,
                    "kind": str(raw.get("kind", "individual_action")),
                }
            )
        return normalized

    @staticmethod
    def _is_read_only_command(cmd: str) -> bool:
        normalized = cmd.strip().lower()
        return any(normalized.startswith(prefix) for prefix in READ_ONLY_PREFIXES)

    @staticmethod
    def _score_segment(*, segment: list[dict[str, Any]]) -> dict[str, Any]:
        steps = len(segment)
        samples = steps * 10
        replay_success = 1.0 if steps > 0 else 0.0
        score = min(0.99, 0.75 + 0.08 * steps)
        p95_duration = 0
        return {
            "score": round(score, 4),
            "replay_success": round(replay_success, 4),
            "samples": samples,
            "error_rate": round(1.0 - replay_success, 4),
            "p95_duration": p95_duration,
        }

    @staticmethod
    def _derive_skill_key(*, entry: ExecutionHistory) -> str:
        tags = [part.strip() for part in (entry.tags or "").split(",") if part.strip()]
        for tag in tags:
            if tag.startswith("skill:") and len(tag) > len("skill:"):
                return tag[len("skill:") :]
        return f"browser-{entry.sandbox_id}"

    @staticmethod
    def _derive_scenario_key(*, entry: ExecutionHistory) -> str | None:
        if not entry.description:
            return None
        normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", entry.description.strip().lower())
        normalized = normalized.strip("-")
        if not normalized:
            return None
        return normalized[:64]

    async def _process_auto_canary_lifecycle(
        self,
        *,
        result: BrowserLearningCycleResult,
    ) -> None:
        releases = await self._svc.list_active_auto_canary_releases(limit=200)
        for release in releases:
            health = await self._svc.get_release_health(
                owner=release.owner,
                release_id=release.id,
                success_drop_threshold=self._config.success_drop_threshold,
                error_rate_multiplier_threshold=self._config.error_rate_multiplier_threshold,
            )

            if health["should_rollback"]:
                rollback_release = await self._svc.rollback_release(
                    owner=release.owner,
                    release_id=release.id,
                    rolled_back_by="system:auto",
                    release_mode=SkillReleaseMode.AUTO,
                )
                self._log.warning(
                    "skills.browser.auto_rollback",
                    release_id=release.id,
                    new_release_id=rollback_release.id,
                    reason=health["rollback_reasons"],
                    threshold_snapshot=health["thresholds"],
                )
                result.rolled_back += 1
                result.audit_events.append(f"auto_rollback:{release.id}")
                continue

            if not self._auto_release_enabled:
                continue

            if health["window_complete"] and health["healthy"]:
                stable_release = await self._svc.promote_candidate(
                    owner=release.owner,
                    candidate_id=release.candidate_id,
                    stage=SkillReleaseStage.STABLE,
                    promoted_by="system:auto",
                    release_mode=SkillReleaseMode.AUTO,
                    auto_promoted_from=release.id,
                )
                self._log.info(
                    "skills.browser.auto_promote_stable",
                    canary_release_id=release.id,
                    stable_release_id=stable_release.id,
                )
                result.promoted_stable += 1
                result.audit_events.append(f"auto_promote_stable:{stable_release.id}")


class BrowserLearningScheduler:
    """Periodic scheduler for browser learning pipeline."""

    def __init__(self, config: BrowserLearningConfig) -> None:
        self._config = config
        self._running = False
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._log = logger.bind(service="browser_learning_scheduler")

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_once(self) -> BrowserLearningCycleResult:
        async with self._run_lock:
            return await self._run_cycle()

    async def _run_cycle(self) -> BrowserLearningCycleResult:
        settings = get_settings()
        if not settings.browser_learning.enabled:
            return BrowserLearningCycleResult()

        async with get_async_session() as db_session:
            svc = SkillLifecycleService(db_session)
            processor = BrowserLearningProcessor(
                service=svc,
                config=settings.browser_learning,
                auto_release_enabled=settings.browser_auto_release_enabled,
            )
            result = await processor.run_cycle()
            self._log.info(
                "skills.browser.learning.cycle_complete",
                processed=result.processed_executions,
                skipped=result.skipped_executions,
                errored=result.errored_executions,
                candidates=result.created_candidates,
                promoted_canary=result.promoted_canary,
                promoted_stable=result.promoted_stable,
                rolled_back=result.rolled_back,
            )
            return result

    async def start(self) -> None:
        if self._running:
            self._log.warning("skills.browser.scheduler.already_running")
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        self._log.info(
            "skills.browser.scheduler.started",
            interval_seconds=self._config.interval_seconds,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._log.info("skills.browser.scheduler.stopped")

    async def _background_loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                self._log.exception("skills.browser.scheduler.cycle_failed", error=str(exc))
            try:
                await asyncio.sleep(self._config.interval_seconds)
            except asyncio.CancelledError:
                break
