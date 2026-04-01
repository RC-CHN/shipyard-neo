"""Skill lifecycle management for Bay SDK."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

from shipyard_neo.types import (
    GoalDeclaration,
    SkillCandidateInfo,
    SkillCandidateList,
    SkillCandidateStatus,
    SkillEvaluationInfo,
    SkillPayloadCreateInfo,
    SkillPayloadInfo,
    SkillReleaseHealth,
    SkillReleaseInfo,
    SkillReleaseList,
    SkillReleaseStage,
    SkillView,
    _SkillPayloadCreateRequest,
)

if TYPE_CHECKING:
    from shipyard_neo._http import HTTPClient


class SkillManager:
    """Skill lifecycle API client."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    async def create_payload(
        self,
        *,
        payload: dict[str, Any] | list[Any] | str,
        kind: str = "generic",
    ) -> SkillPayloadCreateInfo:
        normalized_payload: dict[str, Any] | list[Any] | str = payload
        if isinstance(payload, str):
            try:
                normalized_payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "payload must be a JSON object/array or a JSON string representing one"
                ) from exc

        if not isinstance(normalized_payload, (dict, list)):
            raise ValueError(
                "payload must be a JSON object/array or a JSON string representing one"
            )

        body = _SkillPayloadCreateRequest(
            payload=cast(dict[str, Any] | list[Any], normalized_payload),
            kind=kind,
        ).model_dump(exclude_none=True)
        response = await self._http.post(
            "/v1/skills/payloads",
            json=body,
        )
        return SkillPayloadCreateInfo.model_validate(response)

    async def get_payload(self, payload_ref: str) -> SkillPayloadInfo:
        response = await self._http.get(f"/v1/skills/payloads/{payload_ref}")
        return SkillPayloadInfo.model_validate(response)

    async def create_candidate(
        self,
        *,
        skill_key: str,
        source_execution_ids: list[str],
        scenario_key: str | None = None,
        payload_ref: str | None = None,
        summary: str | None = None,
        usage_notes: str | None = None,
        preconditions: list[str] | dict[str, Any] | None = None,
        postconditions: list[str] | dict[str, Any] | None = None,
    ) -> SkillCandidateInfo:
        body = {
            "skill_key": skill_key,
            "source_execution_ids": source_execution_ids,
            "scenario_key": scenario_key,
            "payload_ref": payload_ref,
            "summary": summary,
            "usage_notes": usage_notes,
            "preconditions": preconditions,
            "postconditions": postconditions,
        }
        # Keep payload compatible with API: omit null fields.
        body = {k: v for k, v in body.items() if v is not None}

        response = await self._http.post(
            "/v1/skills/candidates",
            json=body,
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
        body = {
            "passed": passed,
            "score": score,
            "benchmark_id": benchmark_id,
            "report": report,
        }
        body = {k: v for k, v in body.items() if v is not None}

        response = await self._http.post(
            f"/v1/skills/candidates/{candidate_id}/evaluate",
            json=body,
        )
        return SkillEvaluationInfo.model_validate(response)

    async def promote_candidate(
        self,
        candidate_id: str,
        *,
        stage: SkillReleaseStage | str = SkillReleaseStage.CANARY,
        upgrade_of_release_id: str | None = None,
        upgrade_reason: str | None = None,
        change_summary: str | None = None,
    ) -> SkillReleaseInfo:
        stage_value = stage.value if isinstance(stage, SkillReleaseStage) else stage
        payload = {
            "stage": stage_value,
            "upgrade_of_release_id": upgrade_of_release_id,
            "upgrade_reason": upgrade_reason,
            "change_summary": change_summary,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        response = await self._http.post(
            f"/v1/skills/candidates/{candidate_id}/promote",
            json=payload,
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

    async def delete_release(
        self,
        release_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        # Server-side behavior may allow deleting an active release;
        # callers should rely on API response semantics.
        response = await self._http.delete(
            f"/v1/skills/releases/{release_id}",
            json={"reason": reason} if reason is not None else {},
        )
        return response

    async def delete_candidate(
        self,
        candidate_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        response = await self._http.delete(
            f"/v1/skills/candidates/{candidate_id}",
            json={"reason": reason} if reason is not None else {},
        )
        return response

    async def rollback_release(self, release_id: str) -> SkillReleaseInfo:
        response = await self._http.post(f"/v1/skills/releases/{release_id}/rollback")
        return SkillReleaseInfo.model_validate(response)

    async def get_release_health(self, release_id: str) -> SkillReleaseHealth:
        response = await self._http.get(f"/v1/skills/releases/{release_id}/health")
        return SkillReleaseHealth.model_validate(response)

    # ------------------------------------------------------------------
    # Intent-driven interface (evolution system)
    # ------------------------------------------------------------------

    async def declare_goal(
        self,
        *,
        skill_key: str,
        goal: str,
    ) -> GoalDeclaration:
        """Declare the goal for a skill key.

        The system uses this goal to drive skill evolution and generate
        evaluation criteria. Call once when introducing a new skill, or
        whenever the goal changes. This is an upsert operation.
        """
        response = await self._http.post(
            "/v1/skills/goals",
            json={"skill_key": skill_key, "goal": goal},
        )
        return GoalDeclaration.model_validate(response)

    async def get_active(self, skill_key: str) -> SkillView | None:
        """Get the currently active skill release.

        Returns a SkillView whose ``content`` field can be passed directly
        to an LLM as execution instructions. Returns None if no active
        release exists yet.
        """
        from shipyard_neo.errors import NotFoundError

        try:
            response = await self._http.get(f"/v1/skills/{skill_key}/active")
        except NotFoundError:
            return None
        return SkillView.model_validate(response)

    async def report_outcome(
        self,
        *,
        skill_key: str,
        release_id: str,
        outcome: Literal["success", "failure", "partial"],
        reasoning: str,
        execution_id: str | None = None,
        signals: dict[str, Any] | None = None,
    ) -> None:
        """Report the outcome of executing a skill.

        ``reasoning`` is required — explain why the execution succeeded or
        failed. This text is stored as the evolution signal feed: failures
        generate reflection memory that improves future mutations, successes
        reinforce the current approach.
        """
        body: dict[str, Any] = {
            "skill_key": skill_key,
            "release_id": release_id,
            "outcome": outcome,
            "reasoning": reasoning,
        }
        if execution_id is not None:
            body["execution_id"] = execution_id
        if signals is not None:
            body["signals"] = signals
        await self._http.post("/v1/skills/outcomes", json=body)
