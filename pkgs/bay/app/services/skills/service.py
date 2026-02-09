"""Skill lifecycle service.

Provides Bay control-plane operations for:
- execution history persistence and query
- candidate lifecycle
- evaluation records
- promotion and rollback
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.errors import ConflictError, NotFoundError, ValidationError
from app.models.skill import (
    ExecutionHistory,
    ExecutionType,
    SkillCandidate,
    SkillCandidateStatus,
    SkillEvaluation,
    SkillRelease,
    SkillReleaseStage,
)


class SkillLifecycleService:
    """Service for skill learning lifecycle operations."""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    @staticmethod
    def _normalize_tags(tags: str | None) -> str | None:
        if tags is None:
            return None
        normalized = [t.strip() for t in tags.split(",") if t.strip()]
        if not normalized:
            return None
        # Stable order for deterministic matching output.
        return ",".join(sorted(set(normalized)))

    @staticmethod
    def _split_csv(value: str | None) -> list[str]:
        if not value:
            return []
        return [part for part in value.split(",") if part]

    @staticmethod
    def _join_csv(values: list[str]) -> str:
        return ",".join(values)

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
        description: str | None = None,
        tags: str | None = None,
    ) -> ExecutionHistory:
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
            description=description,
            tags=self._normalize_tags(tags),
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
            filters.append(
                or_(
                    *[ExecutionHistory.tags.ilike(f"%{tag}%") for tag in tag_list]
                )
            )

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
            status=SkillCandidateStatus.DRAFT,
            created_by=created_by,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        self._db.add(candidate)
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

    async def promote_candidate(
        self,
        *,
        owner: str,
        candidate_id: str,
        stage: SkillReleaseStage = SkillReleaseStage.CANARY,
        promoted_by: str | None = None,
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

        # Deactivate existing active release for this skill key.
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
            promoted_by=promoted_by,
            promoted_at=datetime.utcnow(),
        )
        self._db.add(release)

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

    async def rollback_release(
        self,
        *,
        owner: str,
        release_id: str,
        rolled_back_by: str | None = None,
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
            promoted_by=rolled_back_by,
            promoted_at=datetime.utcnow(),
            rollback_of=current.id,
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
