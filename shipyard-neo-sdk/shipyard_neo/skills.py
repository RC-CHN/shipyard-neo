"""Skill lifecycle management for Bay SDK."""

from __future__ import annotations

from shipyard_neo.types import (
    SkillCandidateInfo,
    SkillCandidateList,
    SkillCandidateStatus,
    SkillEvaluationInfo,
    SkillReleaseInfo,
    SkillReleaseList,
    SkillReleaseStage,
)


class SkillManager:
    """Skill lifecycle API client."""

    def __init__(self, http) -> None:
        self._http = http

    async def create_candidate(
        self,
        *,
        skill_key: str,
        source_execution_ids: list[str],
        scenario_key: str | None = None,
        payload_ref: str | None = None,
    ) -> SkillCandidateInfo:
        response = await self._http.post(
            "/v1/skills/candidates",
            json={
                "skill_key": skill_key,
                "source_execution_ids": source_execution_ids,
                "scenario_key": scenario_key,
                "payload_ref": payload_ref,
            },
        )
        return SkillCandidateInfo.model_validate(response)

    async def list_candidates(
        self,
        *,
        status: SkillCandidateStatus | str | None = None,
        skill_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> SkillCandidateList:
        status_value = status.value if isinstance(status, SkillCandidateStatus) else status
        response = await self._http.get(
            "/v1/skills/candidates",
            params={
                "status": status_value,
                "skill_key": skill_key,
                "limit": limit,
                "offset": offset,
            },
        )
        return SkillCandidateList.model_validate(response)

    async def get_candidate(self, candidate_id: str) -> SkillCandidateInfo:
        response = await self._http.get(f"/v1/skills/candidates/{candidate_id}")
        return SkillCandidateInfo.model_validate(response)

    async def evaluate_candidate(
        self,
        candidate_id: str,
        *,
        passed: bool,
        score: float | None = None,
        benchmark_id: str | None = None,
        report: str | None = None,
    ) -> SkillEvaluationInfo:
        response = await self._http.post(
            f"/v1/skills/candidates/{candidate_id}/evaluate",
            json={
                "passed": passed,
                "score": score,
                "benchmark_id": benchmark_id,
                "report": report,
            },
        )
        return SkillEvaluationInfo.model_validate(response)

    async def promote_candidate(
        self,
        candidate_id: str,
        *,
        stage: SkillReleaseStage | str = SkillReleaseStage.CANARY,
    ) -> SkillReleaseInfo:
        stage_value = stage.value if isinstance(stage, SkillReleaseStage) else stage
        response = await self._http.post(
            f"/v1/skills/candidates/{candidate_id}/promote",
            json={"stage": stage_value},
        )
        return SkillReleaseInfo.model_validate(response)

    async def list_releases(
        self,
        *,
        skill_key: str | None = None,
        active_only: bool = False,
        stage: SkillReleaseStage | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> SkillReleaseList:
        stage_value = stage.value if isinstance(stage, SkillReleaseStage) else stage
        response = await self._http.get(
            "/v1/skills/releases",
            params={
                "skill_key": skill_key,
                "active_only": active_only,
                "stage": stage_value,
                "limit": limit,
                "offset": offset,
            },
        )
        return SkillReleaseList.model_validate(response)

    async def rollback_release(self, release_id: str) -> SkillReleaseInfo:
        response = await self._http.post(f"/v1/skills/releases/{release_id}/rollback")
        return SkillReleaseInfo.model_validate(response)
