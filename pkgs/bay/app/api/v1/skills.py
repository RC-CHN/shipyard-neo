"""Skill lifecycle control-plane endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.dependencies import AuthDep, SkillLifecycleServiceDep
from app.errors import ValidationError
from app.models.skill import SkillCandidateStatus, SkillReleaseStage

router = APIRouter()


class SkillCandidateCreateRequest(BaseModel):
    """Create candidate request."""

    skill_key: str
    source_execution_ids: list[str]
    scenario_key: str | None = None
    payload_ref: str | None = None
    summary: str | None = None
    usage_notes: str | None = None
    preconditions: dict[str, Any] | None = None
    postconditions: dict[str, Any] | None = None


class SkillCandidateResponse(BaseModel):
    """Candidate response."""

    id: str
    skill_key: str
    scenario_key: str | None
    payload_ref: str | None
    skill_type: str
    auto_release_eligible: bool
    auto_release_reason: str | None
    summary: str | None
    usage_notes: str | None
    preconditions: dict[str, Any] | None
    postconditions: dict[str, Any] | None
    source_execution_ids: list[str]
    status: str
    latest_score: float | None
    latest_pass: bool | None
    last_evaluated_at: datetime | None
    promotion_release_id: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
    deleted_at: datetime | None
    deleted_by: str | None
    delete_reason: str | None


class SkillCandidateListResponse(BaseModel):
    """Candidate list response."""

    items: list[SkillCandidateResponse]
    total: int


class SkillEvaluationRequest(BaseModel):
    """Evaluate candidate request."""

    passed: bool
    score: float | None = None
    benchmark_id: str | None = None
    report: str | None = None


class SkillEvaluationResponse(BaseModel):
    """Evaluation response."""

    id: str
    candidate_id: str
    benchmark_id: str | None
    score: float | None
    passed: bool
    report: str | None
    evaluated_by: str | None
    created_at: datetime


class SkillPromotionRequest(BaseModel):
    """Promotion request."""

    stage: str = SkillReleaseStage.CANARY.value
    upgrade_of_release_id: str | None = None
    upgrade_reason: str | None = None
    change_summary: str | None = None


class SkillReleaseResponse(BaseModel):
    """Release response."""

    id: str
    skill_key: str
    candidate_id: str
    version: int
    stage: str
    is_active: bool
    release_mode: str
    promoted_by: str | None
    promoted_at: datetime
    rollback_of: str | None
    auto_promoted_from: str | None
    health_window_end_at: datetime | None
    upgrade_of_release_id: str | None
    upgrade_reason: str | None
    change_summary: str | None
    is_deleted: bool
    deleted_at: datetime | None
    deleted_by: str | None
    delete_reason: str | None


class SkillReleaseHealthResponse(BaseModel):
    """Release health response."""

    release_id: str
    skill_key: str
    stage: str
    window_start_at: datetime
    window_end_at: datetime
    window_complete: bool
    samples: int
    success_rate: float
    error_rate: float
    p95_duration: int
    baseline_success_rate: float
    baseline_error_rate: float
    baseline_samples: int
    success_drop: float
    error_rate_multiplier: float
    healthy: bool
    should_rollback: bool
    rollback_reasons: list[str]
    thresholds: dict[str, float]


class SkillReleaseListResponse(BaseModel):
    """Release list response."""

    items: list[SkillReleaseResponse]
    total: int


class SkillPayloadCreateRequest(BaseModel):
    """Create payload request."""

    payload: dict[str, Any] | list[Any]
    kind: str = Field(default="generic", min_length=1)


class SkillPayloadCreateResponse(BaseModel):
    """Create payload response."""

    payload_ref: str
    kind: str


class SkillPayloadResponse(BaseModel):
    """Payload lookup response."""

    payload_ref: str
    kind: str
    payload: dict[str, Any] | list[Any]


class SkillDeleteRequest(BaseModel):
    """Delete request."""

    reason: str | None = None


class SkillDeleteResponse(BaseModel):
    """Delete response."""

    id: str
    deleted_at: datetime
    deleted_by: str | None
    delete_reason: str | None


def _json_field_to_obj(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _candidate_to_response(candidate) -> SkillCandidateResponse:
    source_execution_ids = [item for item in candidate.source_execution_ids.split(",") if item]
    return SkillCandidateResponse(
        id=candidate.id,
        skill_key=candidate.skill_key,
        scenario_key=candidate.scenario_key,
        payload_ref=candidate.payload_ref,
        skill_type=candidate.skill_type.value,
        auto_release_eligible=candidate.auto_release_eligible,
        auto_release_reason=candidate.auto_release_reason,
        summary=candidate.summary,
        usage_notes=candidate.usage_notes,
        preconditions=_json_field_to_obj(candidate.preconditions_json),
        postconditions=_json_field_to_obj(candidate.postconditions_json),
        source_execution_ids=source_execution_ids,
        status=candidate.status.value,
        latest_score=candidate.latest_score,
        latest_pass=candidate.latest_pass,
        last_evaluated_at=candidate.last_evaluated_at,
        promotion_release_id=candidate.promotion_release_id,
        created_by=candidate.created_by,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
        is_deleted=candidate.is_deleted,
        deleted_at=candidate.deleted_at,
        deleted_by=candidate.deleted_by,
        delete_reason=candidate.delete_reason,
    )


def _evaluation_to_response(evaluation) -> SkillEvaluationResponse:
    return SkillEvaluationResponse(
        id=evaluation.id,
        candidate_id=evaluation.candidate_id,
        benchmark_id=evaluation.benchmark_id,
        score=evaluation.score,
        passed=evaluation.passed,
        report=evaluation.report,
        evaluated_by=evaluation.evaluated_by,
        created_at=evaluation.created_at,
    )


def _release_to_response(release) -> SkillReleaseResponse:
    return SkillReleaseResponse(
        id=release.id,
        skill_key=release.skill_key,
        candidate_id=release.candidate_id,
        version=release.version,
        stage=release.stage.value,
        is_active=release.is_active,
        release_mode=release.release_mode.value,
        promoted_by=release.promoted_by,
        promoted_at=release.promoted_at,
        rollback_of=release.rollback_of,
        auto_promoted_from=release.auto_promoted_from,
        health_window_end_at=release.health_window_end_at,
        upgrade_of_release_id=release.upgrade_of_release_id,
        upgrade_reason=release.upgrade_reason,
        change_summary=release.change_summary,
        is_deleted=release.is_deleted,
        deleted_at=release.deleted_at,
        deleted_by=release.deleted_by,
        delete_reason=release.delete_reason,
    )


@router.post("/payloads", response_model=SkillPayloadCreateResponse, status_code=201)
async def create_payload(
    request: SkillPayloadCreateRequest,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillPayloadCreateResponse:
    blob = await skill_svc.create_artifact_blob(
        owner=owner,
        kind=request.kind,
        payload=request.payload,
    )
    return SkillPayloadCreateResponse(
        payload_ref=skill_svc.make_blob_ref(blob.id),
        kind=blob.kind,
    )


@router.get("/payloads/{payload_ref:path}", response_model=SkillPayloadResponse)
async def get_payload(
    payload_ref: str,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillPayloadResponse:
    blob, payload = await skill_svc.get_payload_with_blob_by_ref(
        owner=owner,
        payload_ref=payload_ref,
    )
    return SkillPayloadResponse(
        payload_ref=payload_ref,
        kind=blob.kind,
        payload=payload,
    )


@router.post("/candidates", response_model=SkillCandidateResponse, status_code=201)
async def create_candidate(
    request: SkillCandidateCreateRequest,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillCandidateResponse:
    candidate = await skill_svc.create_candidate(
        owner=owner,
        skill_key=request.skill_key,
        source_execution_ids=request.source_execution_ids,
        scenario_key=request.scenario_key,
        payload_ref=request.payload_ref,
        summary=request.summary,
        usage_notes=request.usage_notes,
        preconditions=request.preconditions,
        postconditions=request.postconditions,
        created_by=owner,
    )
    return _candidate_to_response(candidate)


@router.get("/candidates", response_model=SkillCandidateListResponse)
async def list_candidates(
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
    status: str | None = Query(None),
    skill_key: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> SkillCandidateListResponse:
    if status:
        try:
            parsed_status = SkillCandidateStatus(status)
        except ValueError as exc:
            raise ValidationError(f"Invalid candidate status: {status}") from exc
    else:
        parsed_status = None
    items, total = await skill_svc.list_candidates(
        owner=owner,
        status=parsed_status,
        skill_key=skill_key,
        limit=limit,
        offset=offset,
    )
    return SkillCandidateListResponse(
        items=[_candidate_to_response(item) for item in items],
        total=total,
    )


@router.get("/candidates/{candidate_id}", response_model=SkillCandidateResponse)
async def get_candidate(
    candidate_id: str,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillCandidateResponse:
    candidate = await skill_svc.get_candidate(owner=owner, candidate_id=candidate_id)
    return _candidate_to_response(candidate)


@router.post("/candidates/{candidate_id}/evaluate", response_model=SkillEvaluationResponse)
async def evaluate_candidate(
    candidate_id: str,
    request: SkillEvaluationRequest,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillEvaluationResponse:
    _candidate, evaluation = await skill_svc.evaluate_candidate(
        owner=owner,
        candidate_id=candidate_id,
        passed=request.passed,
        score=request.score,
        benchmark_id=request.benchmark_id,
        report=request.report,
        evaluated_by=owner,
    )
    return _evaluation_to_response(evaluation)


@router.post("/candidates/{candidate_id}/promote", response_model=SkillReleaseResponse)
async def promote_candidate(
    candidate_id: str,
    request: SkillPromotionRequest,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillReleaseResponse:
    try:
        stage = SkillReleaseStage(request.stage)
    except ValueError as exc:
        raise ValidationError(f"Invalid release stage: {request.stage}") from exc
    release = await skill_svc.promote_candidate(
        owner=owner,
        candidate_id=candidate_id,
        stage=stage,
        promoted_by=owner,
        upgrade_of_release_id=request.upgrade_of_release_id,
        upgrade_reason=request.upgrade_reason,
        change_summary=request.change_summary,
    )
    return _release_to_response(release)


@router.get("/releases", response_model=SkillReleaseListResponse)
async def list_releases(
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
    skill_key: str | None = Query(None),
    active_only: bool = Query(False),
    stage: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> SkillReleaseListResponse:
    if stage:
        try:
            parsed_stage = SkillReleaseStage(stage)
        except ValueError as exc:
            raise ValidationError(f"Invalid release stage: {stage}") from exc
    else:
        parsed_stage = None
    items, total = await skill_svc.list_releases(
        owner=owner,
        skill_key=skill_key,
        active_only=active_only,
        stage=parsed_stage,
        limit=limit,
        offset=offset,
    )
    return SkillReleaseListResponse(
        items=[_release_to_response(item) for item in items],
        total=total,
    )


@router.get("/releases/{release_id}/health", response_model=SkillReleaseHealthResponse)
async def get_release_health(
    release_id: str,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillReleaseHealthResponse:
    health = await skill_svc.get_release_health(owner=owner, release_id=release_id)
    return SkillReleaseHealthResponse(**health)


@router.delete("/releases/{release_id}", response_model=SkillDeleteResponse)
async def delete_release(
    release_id: str,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
    request: SkillDeleteRequest | None = None,
) -> SkillDeleteResponse:
    deleted = await skill_svc.delete_release(
        owner=owner,
        release_id=release_id,
        deleted_by=owner,
        reason=(request.reason if request is not None else None),
    )
    return SkillDeleteResponse(
        id=deleted.id,
        deleted_at=deleted.deleted_at,
        deleted_by=deleted.deleted_by,
        delete_reason=deleted.delete_reason,
    )


@router.delete("/candidates/{candidate_id}", response_model=SkillDeleteResponse)
async def delete_candidate(
    candidate_id: str,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
    request: SkillDeleteRequest | None = None,
) -> SkillDeleteResponse:
    deleted = await skill_svc.delete_candidate(
        owner=owner,
        candidate_id=candidate_id,
        deleted_by=owner,
        reason=(request.reason if request is not None else None),
    )
    return SkillDeleteResponse(
        id=deleted.id,
        deleted_at=deleted.deleted_at,
        deleted_by=deleted.deleted_by,
        delete_reason=deleted.delete_reason,
    )


@router.post("/releases/{release_id}/rollback", response_model=SkillReleaseResponse)
async def rollback_release(
    release_id: str,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> SkillReleaseResponse:
    release = await skill_svc.rollback_release(
        owner=owner,
        release_id=release_id,
        rolled_back_by=owner,
    )
    return _release_to_response(release)
