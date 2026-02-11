"""Skill lifecycle service.

Provides Bay control-plane operations for:
- execution evidence persistence and query
- trace payload blob storage
- candidate lifecycle
- evaluation and release operations
- release health aggregation
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.skill import (
    ArtifactBlob,
    ExecutionHistory,
    ExecutionType,
    LearnStatus,
    SkillCandidate,
    SkillCandidateStatus,
    SkillEvaluation,
    SkillRelease,
    SkillReleaseMode,
    SkillReleaseStage,
    SkillType,
)


class SkillLifecycleService:
    """Service for skill learning lifecycle operations."""

    BLOB_REF_PREFIX = "blob:"

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    @staticmethod
    def _normalize_tags(tags: str | None) -> str | None:
        if tags is None:
            return None
        normalized = [t.strip() for t in tags.split(",") if t.strip()]
        if not normalized:
            return None
        return ",".join(sorted(set(normalized)))

    @staticmethod
    def _split_csv(value: str | None) -> list[str]:
        if not value:
            return []
        return [part for part in value.split(",") if part]

    @staticmethod
    def _join_csv(values: list[str]) -> str:
        return ",".join(values)

    @classmethod
    def _make_blob_ref(cls, blob_id: str) -> str:
        return f"{cls.BLOB_REF_PREFIX}{blob_id}"

    @classmethod
    def make_blob_ref(cls, blob_id: str) -> str:
        return cls._make_blob_ref(blob_id)

    @classmethod
    def _parse_blob_ref(cls, payload_ref: str) -> str:
        if not payload_ref.startswith(cls.BLOB_REF_PREFIX):
            raise ValidationError(f"Unsupported payload_ref: {payload_ref}")
        blob_id = payload_ref[len(cls.BLOB_REF_PREFIX) :]
        if not blob_id:
            raise ValidationError(f"Invalid payload_ref: {payload_ref}")
        return blob_id

    @classmethod
    def merge_tags(cls, *tags: str | None) -> str | None:
        merged: list[str] = []
        for raw in tags:
            if raw is None:
                continue
            merged.extend([part.strip() for part in raw.split(",") if part.strip()])
        if not merged:
            return None
        return ",".join(sorted(set(merged)))

    # ---------------------------------------------------------------------
    # Artifact blob storage
    # ---------------------------------------------------------------------

    async def create_artifact_blob(
        self,
        *,
        owner: str,
        payload: dict[str, Any] | list[Any],
        kind: str = "generic",
    ) -> ArtifactBlob:
        blob = ArtifactBlob(
            id=f"blob-{uuid.uuid4().hex[:12]}",
            owner=owner,
            kind=kind,
            payload_json=json.dumps(payload, ensure_ascii=False),
            created_at=datetime.utcnow(),
        )
        self._db.add(blob)
        await self._db.commit()
        await self._db.refresh(blob)
        return blob

    async def get_artifact_blob(self, *, owner: str, blob_id: str) -> ArtifactBlob:
        result = await self._db.execute(
            select(ArtifactBlob).where(
                ArtifactBlob.id == blob_id,
                ArtifactBlob.owner == owner,
            )
        )
        blob = result.scalars().first()
        if blob is None:
            raise NotFoundError(f"Artifact blob not found: {blob_id}")
        return blob

    async def get_artifact_blob_by_ref(
        self,
        *,
        owner: str,
        payload_ref: str,
    ) -> ArtifactBlob:
        blob_id = self._parse_blob_ref(payload_ref)
        return await self.get_artifact_blob(owner=owner, blob_id=blob_id)

    async def get_payload_with_blob_by_ref(
        self,
        *,
        owner: str,
        payload_ref: str,
    ) -> tuple[ArtifactBlob, dict[str, Any] | list[Any]]:
        blob = await self.get_artifact_blob_by_ref(owner=owner, payload_ref=payload_ref)
        try:
            payload = json.loads(blob.payload_json)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid payload JSON in blob: {blob.id}") from exc
        if not isinstance(payload, (dict, list)):
            raise ValidationError(f"Unsupported payload type in blob: {blob.id}")
        return blob, payload

    async def get_payload_by_ref(
        self,
        *,
        owner: str,
        payload_ref: str | None,
    ) -> dict[str, Any] | list[Any] | None:
        if payload_ref is None:
            return None
        _blob, payload = await self.get_payload_with_blob_by_ref(
            owner=owner,
            payload_ref=payload_ref,
        )
        return payload

    # ---------------------------------------------------------------------
    # Execution history
    # ---------------------------------------------------------------------

    async def create_execution(
        self,
        *,
        owner: str,
        sandbox_id: str,
        exec_type: ExecutionType,
        code: str,
        success: bool,
        execution_time_ms: int,
        session_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        payload_ref: str | None = None,
        description: str | None = None,
        tags: str | None = None,
        learn_enabled: bool = False,
        learn_status: LearnStatus | None = None,
        learn_error: str | None = None,
        learn_processed_at: datetime | None = None,
    ) -> ExecutionHistory:
        normalized_learn_status = learn_status
        if normalized_learn_status is None and learn_enabled:
            normalized_learn_status = LearnStatus.PENDING

        entry = ExecutionHistory(
            id=f"exec-{uuid.uuid4().hex[:12]}",
            owner=owner,
            sandbox_id=sandbox_id,
            session_id=session_id,
            exec_type=exec_type,
            code=code,
            success=success,
            execution_time_ms=max(execution_time_ms, 0),
            output=output,
            error=error,
            payload_ref=payload_ref,
            description=description,
            tags=self._normalize_tags(tags),
            learn_enabled=learn_enabled,
            learn_status=normalized_learn_status,
            learn_error=learn_error,
            learn_processed_at=learn_processed_at,
            created_at=datetime.utcnow(),
        )
        self._db.add(entry)
        await self._db.commit()
        await self._db.refresh(entry)
        return entry

    async def get_execution(
        self,
        *,
        owner: str,
        sandbox_id: str,
        execution_id: str,
    ) -> ExecutionHistory:
        result = await self._db.execute(
            select(ExecutionHistory).where(
                ExecutionHistory.id == execution_id,
                ExecutionHistory.owner == owner,
                ExecutionHistory.sandbox_id == sandbox_id,
            )
        )
        entry = result.scalars().first()
        if entry is None:
            raise NotFoundError(f"Execution not found: {execution_id}")
        return entry

    async def get_execution_by_id(
        self,
        *,
        owner: str,
        execution_id: str,
    ) -> ExecutionHistory:
        result = await self._db.execute(
            select(ExecutionHistory).where(
                ExecutionHistory.id == execution_id,
                ExecutionHistory.owner == owner,
            )
        )
        entry = result.scalars().first()
        if entry is None:
            raise NotFoundError(f"Execution not found: {execution_id}")
        return entry

    async def get_last_execution(
        self,
        *,
        owner: str,
        sandbox_id: str,
        exec_type: ExecutionType | None = None,
    ) -> ExecutionHistory:
        query = select(ExecutionHistory).where(
            ExecutionHistory.owner == owner,
            ExecutionHistory.sandbox_id == sandbox_id,
        )
        if exec_type is not None:
            query = query.where(ExecutionHistory.exec_type == exec_type)

        query = query.order_by(ExecutionHistory.created_at.desc()).limit(1)
        result = await self._db.execute(query)
        entry = result.scalars().first()
        if entry is None:
            raise NotFoundError("No execution history found")
        return entry

    async def list_execution_history(
        self,
        *,
        owner: str,
        sandbox_id: str,
        exec_type: ExecutionType | None = None,
        success_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        tags: str | None = None,
        has_notes: bool = False,
        has_description: bool = False,
    ) -> tuple[list[ExecutionHistory], int]:
        if limit <= 0 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")
        if offset < 0:
            raise ValidationError("offset must be >= 0")

        filters = [
            ExecutionHistory.owner == owner,
            ExecutionHistory.sandbox_id == sandbox_id,
        ]

        if exec_type is not None:
            filters.append(ExecutionHistory.exec_type == exec_type)
        if success_only:
            filters.append(ExecutionHistory.success.is_(True))
        if has_notes:
            filters.append(and_(ExecutionHistory.notes.is_not(None), ExecutionHistory.notes != ""))
        if has_description:
            filters.append(
                and_(
                    ExecutionHistory.description.is_not(None),
                    ExecutionHistory.description != "",
                )
            )

        normalized_tags = self._normalize_tags(tags)
        if normalized_tags:
            tag_list = normalized_tags.split(",")
            filters.append(or_(*[ExecutionHistory.tags.ilike(f"%{tag}%") for tag in tag_list]))

        where_clause = and_(*filters)

        total_result = await self._db.execute(
            select(func.count()).select_from(ExecutionHistory).where(where_clause)
        )
        total = int(total_result.scalar_one())

        result = await self._db.execute(
            select(ExecutionHistory)
            .where(where_clause)
            .order_by(ExecutionHistory.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total

    async def annotate_execution(
        self,
        *,
        owner: str,
        sandbox_id: str,
        execution_id: str,
        description: str | None = None,
        tags: str | None = None,
        notes: str | None = None,
    ) -> ExecutionHistory:
        entry = await self.get_execution(
            owner=owner,
            sandbox_id=sandbox_id,
            execution_id=execution_id,
        )

        if description is not None:
            entry.description = description
        if tags is not None:
            entry.tags = self._normalize_tags(tags)
        if notes is not None:
            entry.notes = notes

        await self._db.commit()
        await self._db.refresh(entry)
        return entry

    async def set_execution_learning_status(
        self,
        *,
        execution_id: str,
        status: LearnStatus,
        error: str | None = None,
        processed_at: datetime | None = None,
    ) -> ExecutionHistory:
        result = await self._db.execute(
            select(ExecutionHistory).where(ExecutionHistory.id == execution_id)
        )
        entry = result.scalars().first()
        if entry is None:
            raise NotFoundError(f"Execution not found: {execution_id}")

        entry.learn_status = status
        entry.learn_error = error
        entry.learn_processed_at = processed_at
        await self._db.commit()
        await self._db.refresh(entry)
        return entry

    async def list_pending_browser_learning_executions(
        self,
        *,
        limit: int = 50,
    ) -> list[ExecutionHistory]:
        if limit <= 0 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")

        result = await self._db.execute(
            select(ExecutionHistory)
            .where(
                ExecutionHistory.learn_enabled.is_(True),
                ExecutionHistory.exec_type.in_(
                    [ExecutionType.BROWSER, ExecutionType.BROWSER_BATCH]
                ),
                or_(
                    ExecutionHistory.learn_status.is_(None),
                    ExecutionHistory.learn_status == LearnStatus.PENDING,
                ),
            )
            .order_by(ExecutionHistory.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # ---------------------------------------------------------------------
    # Candidate lifecycle
    # ---------------------------------------------------------------------

    async def create_candidate(
        self,
        *,
        owner: str,
        skill_key: str,
        source_execution_ids: list[str],
        scenario_key: str | None = None,
        payload_ref: str | None = None,
        created_by: str | None = None,
        skill_type: SkillType = SkillType.CODE,
        auto_release_eligible: bool = False,
        auto_release_reason: str | None = None,
    ) -> SkillCandidate:
        if not skill_key.strip():
            raise ValidationError("skill_key must not be empty")
        if not source_execution_ids:
            raise ValidationError("source_execution_ids must not be empty")

        for execution_id in source_execution_ids:
            await self._assert_execution_owned(owner=owner, execution_id=execution_id)

        candidate = SkillCandidate(
            id=f"sc-{uuid.uuid4().hex[:12]}",
            owner=owner,
            skill_key=skill_key.strip(),
            scenario_key=scenario_key,
            payload_ref=payload_ref,
            source_execution_ids=self._join_csv(source_execution_ids),
            skill_type=skill_type,
            auto_release_eligible=auto_release_eligible,
            auto_release_reason=auto_release_reason,
            status=SkillCandidateStatus.DRAFT,
            created_by=created_by,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        self._db.add(candidate)
        await self._db.commit()
        await self._db.refresh(candidate)
        return candidate

    async def update_candidate_auto_release(
        self,
        *,
        owner: str,
        candidate_id: str,
        eligible: bool,
        reason: str | None = None,
    ) -> SkillCandidate:
        candidate = await self.get_candidate(owner=owner, candidate_id=candidate_id)
        candidate.auto_release_eligible = eligible
        candidate.auto_release_reason = reason
        candidate.updated_at = datetime.utcnow()
        await self._db.commit()
        await self._db.refresh(candidate)
        return candidate

    async def get_candidate(self, *, owner: str, candidate_id: str) -> SkillCandidate:
        result = await self._db.execute(
            select(SkillCandidate).where(
                SkillCandidate.id == candidate_id,
                SkillCandidate.owner == owner,
            )
        )
        candidate = result.scalars().first()
        if candidate is None:
            raise NotFoundError(f"Skill candidate not found: {candidate_id}")
        return candidate

    async def list_candidates(
        self,
        *,
        owner: str,
        status: SkillCandidateStatus | None = None,
        skill_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SkillCandidate], int]:
        if limit <= 0 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")
        if offset < 0:
            raise ValidationError("offset must be >= 0")

        filters = [SkillCandidate.owner == owner]
        if status is not None:
            filters.append(SkillCandidate.status == status)
        if skill_key is not None:
            filters.append(SkillCandidate.skill_key == skill_key)

        where_clause = and_(*filters)

        total_result = await self._db.execute(
            select(func.count()).select_from(SkillCandidate).where(where_clause)
        )
        total = int(total_result.scalar_one())

        result = await self._db.execute(
            select(SkillCandidate)
            .where(where_clause)
            .order_by(SkillCandidate.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total

    async def evaluate_candidate(
        self,
        *,
        owner: str,
        candidate_id: str,
        passed: bool,
        score: float | None = None,
        benchmark_id: str | None = None,
        report: str | None = None,
        evaluated_by: str | None = None,
    ) -> tuple[SkillCandidate, SkillEvaluation]:
        candidate = await self.get_candidate(owner=owner, candidate_id=candidate_id)

        candidate.status = SkillCandidateStatus.EVALUATING
        candidate.updated_at = datetime.utcnow()
        await self._db.commit()

        evaluation = SkillEvaluation(
            id=f"se-{uuid.uuid4().hex[:12]}",
            owner=owner,
            candidate_id=candidate.id,
            benchmark_id=benchmark_id,
            score=score,
            passed=passed,
            report=report,
            evaluated_by=evaluated_by,
            created_at=datetime.utcnow(),
        )
        self._db.add(evaluation)

        candidate.latest_score = score
        candidate.latest_pass = passed
        candidate.last_evaluated_at = datetime.utcnow()
        candidate.updated_at = datetime.utcnow()
        if not passed:
            candidate.status = SkillCandidateStatus.REJECTED

        await self._db.commit()
        await self._db.refresh(candidate)
        await self._db.refresh(evaluation)

        return candidate, evaluation

    async def get_latest_evaluation(
        self,
        *,
        owner: str,
        candidate_id: str,
    ) -> SkillEvaluation | None:
        result = await self._db.execute(
            select(SkillEvaluation)
            .where(
                SkillEvaluation.owner == owner,
                SkillEvaluation.candidate_id == candidate_id,
            )
            .order_by(SkillEvaluation.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def promote_candidate(
        self,
        *,
        owner: str,
        candidate_id: str,
        stage: SkillReleaseStage = SkillReleaseStage.CANARY,
        promoted_by: str | None = None,
        release_mode: SkillReleaseMode = SkillReleaseMode.MANUAL,
        auto_promoted_from: str | None = None,
        health_window_end_at: datetime | None = None,
    ) -> SkillRelease:
        candidate = await self.get_candidate(owner=owner, candidate_id=candidate_id)

        if candidate.latest_pass is not True:
            raise ConflictError(
                "Candidate has no passing evaluation; promotion is blocked",
                details={"candidate_id": candidate_id},
            )

        max_version_result = await self._db.execute(
            select(func.max(SkillRelease.version)).where(
                SkillRelease.owner == owner,
                SkillRelease.skill_key == candidate.skill_key,
            )
        )
        max_version = max_version_result.scalar()
        next_version = int(max_version or 0) + 1

        active_result = await self._db.execute(
            select(SkillRelease).where(
                SkillRelease.owner == owner,
                SkillRelease.skill_key == candidate.skill_key,
                SkillRelease.is_active.is_(True),
            )
        )
        for release in active_result.scalars().all():
            release.is_active = False

        release = SkillRelease(
            id=f"sr-{uuid.uuid4().hex[:12]}",
            owner=owner,
            skill_key=candidate.skill_key,
            candidate_id=candidate.id,
            version=next_version,
            stage=stage,
            is_active=True,
            release_mode=release_mode,
            promoted_by=promoted_by,
            promoted_at=datetime.utcnow(),
            auto_promoted_from=auto_promoted_from,
            health_window_end_at=health_window_end_at,
        )
        self._db.add(release)

        if (
            candidate.skill_type == SkillType.BROWSER
            and release_mode == SkillReleaseMode.AUTO
            and stage == SkillReleaseStage.CANARY
        ):
            candidate.status = SkillCandidateStatus.PROMOTED_CANARY
        elif (
            candidate.skill_type == SkillType.BROWSER
            and release_mode == SkillReleaseMode.AUTO
            and stage == SkillReleaseStage.STABLE
        ):
            candidate.status = SkillCandidateStatus.PROMOTED_STABLE
        else:
            candidate.status = SkillCandidateStatus.PROMOTED
        candidate.updated_at = datetime.utcnow()
        candidate.promotion_release_id = release.id

        await self._db.commit()
        await self._db.refresh(release)
        await self._db.refresh(candidate)
        return release

    async def list_releases(
        self,
        *,
        owner: str,
        skill_key: str | None = None,
        active_only: bool = False,
        stage: SkillReleaseStage | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SkillRelease], int]:
        if limit <= 0 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")
        if offset < 0:
            raise ValidationError("offset must be >= 0")

        filters = [SkillRelease.owner == owner]
        if skill_key is not None:
            filters.append(SkillRelease.skill_key == skill_key)
        if active_only:
            filters.append(SkillRelease.is_active.is_(True))
        if stage is not None:
            filters.append(SkillRelease.stage == stage)

        where_clause = and_(*filters)

        total_result = await self._db.execute(
            select(func.count()).select_from(SkillRelease).where(where_clause)
        )
        total = int(total_result.scalar_one())

        result = await self._db.execute(
            select(SkillRelease)
            .where(where_clause)
            .order_by(SkillRelease.promoted_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total

    async def list_active_auto_canary_releases(self, *, limit: int = 200) -> list[SkillRelease]:
        result = await self._db.execute(
            select(SkillRelease)
            .where(
                SkillRelease.stage == SkillReleaseStage.CANARY,
                SkillRelease.is_active.is_(True),
                SkillRelease.release_mode == SkillReleaseMode.AUTO,
            )
            .order_by(SkillRelease.promoted_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_release(self, *, owner: str, release_id: str) -> SkillRelease:
        return await self._get_release(owner=owner, release_id=release_id)

    async def get_active_release(
        self,
        *,
        owner: str,
        skill_key: str,
        stage: SkillReleaseStage | None = None,
    ) -> SkillRelease | None:
        query = select(SkillRelease).where(
            SkillRelease.owner == owner,
            SkillRelease.skill_key == skill_key,
            SkillRelease.is_active.is_(True),
        )
        if stage is not None:
            query = query.where(SkillRelease.stage == stage)
        query = query.order_by(SkillRelease.version.desc()).limit(1)
        result = await self._db.execute(query)
        return result.scalars().first()

    async def rollback_release(
        self,
        *,
        owner: str,
        release_id: str,
        rolled_back_by: str | None = None,
        release_mode: SkillReleaseMode = SkillReleaseMode.MANUAL,
    ) -> SkillRelease:
        current = await self._get_release(owner=owner, release_id=release_id)

        previous_result = await self._db.execute(
            select(SkillRelease)
            .where(
                SkillRelease.owner == owner,
                SkillRelease.skill_key == current.skill_key,
                SkillRelease.version < current.version,
            )
            .order_by(SkillRelease.version.desc())
            .limit(1)
        )
        previous = previous_result.scalars().first()
        if previous is None:
            raise ConflictError(
                "Rollback is unavailable: no previous release exists",
                details={"release_id": release_id},
            )

        max_version_result = await self._db.execute(
            select(func.max(SkillRelease.version)).where(
                SkillRelease.owner == owner,
                SkillRelease.skill_key == current.skill_key,
            )
        )
        next_version = int(max_version_result.scalar() or 0) + 1

        active_result = await self._db.execute(
            select(SkillRelease).where(
                SkillRelease.owner == owner,
                SkillRelease.skill_key == current.skill_key,
                SkillRelease.is_active.is_(True),
            )
        )
        for release in active_result.scalars().all():
            release.is_active = False

        rollback_release = SkillRelease(
            id=f"sr-{uuid.uuid4().hex[:12]}",
            owner=owner,
            skill_key=current.skill_key,
            candidate_id=previous.candidate_id,
            version=next_version,
            stage=previous.stage,
            is_active=True,
            release_mode=release_mode,
            promoted_by=rolled_back_by,
            promoted_at=datetime.utcnow(),
            rollback_of=current.id,
            auto_promoted_from=current.id if release_mode == SkillReleaseMode.AUTO else None,
        )
        self._db.add(rollback_release)

        current_candidate = await self.get_candidate(owner=owner, candidate_id=current.candidate_id)
        current_candidate.status = SkillCandidateStatus.ROLLED_BACK
        current_candidate.updated_at = datetime.utcnow()

        await self._db.commit()
        await self._db.refresh(rollback_release)
        await self._db.refresh(current_candidate)
        return rollback_release

    # ---------------------------------------------------------------------
    # Release health
    # ---------------------------------------------------------------------

    async def get_release_health(
        self,
        *,
        owner: str,
        release_id: str,
        success_drop_threshold: float = 0.03,
        error_rate_multiplier_threshold: float = 2.0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        release = await self._get_release(owner=owner, release_id=release_id)

        now_dt = now or datetime.utcnow()
        window_end = release.health_window_end_at or (release.promoted_at + timedelta(hours=24))
        window_now = min(now_dt, window_end)
        window_complete = now_dt >= window_end

        observed = await self._compute_release_metrics(
            owner=owner,
            release_id=release.id,
            start_at=release.promoted_at,
            end_at=window_now,
        )

        baseline = await self._resolve_baseline_metrics(owner=owner, release=release)

        success_drop = baseline["success_rate"] - observed["success_rate"]
        if baseline["error_rate"] <= 0.0:
            error_rate_multiplier = 1_000_000.0 if observed["error_rate"] > 0.0 else 1.0
        else:
            error_rate_multiplier = observed["error_rate"] / baseline["error_rate"]

        rollback_reasons: list[str] = []
        if observed["samples"] > 0:
            if success_drop > success_drop_threshold:
                rollback_reasons.append("success_rate_drop")
            if error_rate_multiplier > error_rate_multiplier_threshold:
                rollback_reasons.append("error_rate_regression")

        healthy = len(rollback_reasons) == 0 and observed["samples"] > 0

        return {
            "release_id": release.id,
            "skill_key": release.skill_key,
            "stage": release.stage.value,
            "window_start_at": release.promoted_at,
            "window_end_at": window_end,
            "window_complete": window_complete,
            "samples": observed["samples"],
            "success_rate": observed["success_rate"],
            "error_rate": observed["error_rate"],
            "p95_duration": observed["p95_duration"],
            "baseline_success_rate": baseline["success_rate"],
            "baseline_error_rate": baseline["error_rate"],
            "baseline_samples": baseline["samples"],
            "success_drop": success_drop,
            "error_rate_multiplier": error_rate_multiplier,
            "healthy": healthy,
            "should_rollback": observed["samples"] > 0 and len(rollback_reasons) > 0,
            "rollback_reasons": rollback_reasons,
            "thresholds": {
                "success_drop": success_drop_threshold,
                "error_rate_multiplier": error_rate_multiplier_threshold,
            },
        }

    async def _compute_release_metrics(
        self,
        *,
        owner: str,
        release_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, Any]:
        result = await self._db.execute(
            select(ExecutionHistory).where(
                ExecutionHistory.owner == owner,
                ExecutionHistory.created_at >= start_at,
                ExecutionHistory.created_at <= end_at,
                ExecutionHistory.tags.ilike(f"%release:{release_id}%"),
            )
        )
        items = list(result.scalars().all())
        return self._aggregate_execution_metrics(items)

    async def _resolve_baseline_metrics(
        self,
        *,
        owner: str,
        release: SkillRelease,
    ) -> dict[str, Any]:
        previous_result = await self._db.execute(
            select(SkillRelease)
            .where(
                SkillRelease.owner == owner,
                SkillRelease.skill_key == release.skill_key,
                SkillRelease.version < release.version,
            )
            .order_by(SkillRelease.version.desc())
            .limit(1)
        )
        previous = previous_result.scalars().first()
        if previous is None:
            return {"samples": 0, "success_rate": 1.0, "error_rate": 0.0, "p95_duration": 0}

        metrics = await self._compute_release_metrics(
            owner=owner,
            release_id=previous.id,
            start_at=previous.promoted_at,
            end_at=release.promoted_at,
        )
        if metrics["samples"] > 0:
            return metrics

        latest_eval = await self.get_latest_evaluation(
            owner=owner,
            candidate_id=previous.candidate_id,
        )
        if latest_eval is None or latest_eval.report is None:
            return {"samples": 0, "success_rate": 1.0, "error_rate": 0.0, "p95_duration": 0}

        try:
            report_data = json.loads(latest_eval.report)
        except Exception:
            return {"samples": 0, "success_rate": 1.0, "error_rate": 0.0, "p95_duration": 0}

        success_rate = float(report_data.get("replay_success", 1.0))
        error_rate = float(report_data.get("error_rate", max(0.0, 1.0 - success_rate)))
        samples = int(report_data.get("samples", 0))
        return {
            "samples": max(0, samples),
            "success_rate": min(max(success_rate, 0.0), 1.0),
            "error_rate": min(max(error_rate, 0.0), 1.0),
            "p95_duration": int(report_data.get("p95_duration", 0) or 0),
        }

    @staticmethod
    def _aggregate_execution_metrics(items: list[ExecutionHistory]) -> dict[str, Any]:
        samples = len(items)
        if samples == 0:
            return {"samples": 0, "success_rate": 0.0, "error_rate": 0.0, "p95_duration": 0}

        success_count = sum(1 for item in items if item.success)
        error_count = samples - success_count
        success_rate = success_count / samples
        error_rate = error_count / samples

        durations = sorted(max(0, item.execution_time_ms) for item in items)
        p95_index = max(0, math.ceil(0.95 * samples) - 1)
        p95_duration = durations[p95_index]

        return {
            "samples": samples,
            "success_rate": success_rate,
            "error_rate": error_rate,
            "p95_duration": p95_duration,
        }

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    async def _assert_execution_owned(self, *, owner: str, execution_id: str) -> None:
        result = await self._db.execute(
            select(ExecutionHistory.id).where(
                ExecutionHistory.id == execution_id,
                ExecutionHistory.owner == owner,
            )
        )
        if result.first() is None:
            raise ValidationError(f"Execution ID not found or not owned: {execution_id}")

    async def _get_release(self, *, owner: str, release_id: str) -> SkillRelease:
        result = await self._db.execute(
            select(SkillRelease).where(
                SkillRelease.id == release_id,
                SkillRelease.owner == owner,
            )
        )
        release = result.scalars().first()
        if release is None:
            raise NotFoundError(f"Skill release not found: {release_id}")
        return release
