"""Execution history endpoints.

History is stored at Bay control plane and scoped by sandbox ownership.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.dependencies import AuthDep, SandboxManagerDep, SkillLifecycleServiceDep
from app.models.skill import ExecutionType

router = APIRouter()


class ExecutionHistoryEntryResponse(BaseModel):
    """Execution evidence entry response."""

    id: str
    session_id: str | None
    exec_type: str
    code: str
    success: bool
    execution_time_ms: int
    output: str | None = None
    error: str | None = None
    description: str | None = None
    tags: str | None = None
    notes: str | None = None
    created_at: datetime


class ExecutionHistoryResponse(BaseModel):
    """Execution history query response."""

    entries: list[ExecutionHistoryEntryResponse]
    total: int


class AnnotateExecutionRequest(BaseModel):
    """Patch payload for execution annotation."""

    description: str | None = None
    tags: str | None = None
    notes: str | None = None


def _to_entry_response(entry) -> ExecutionHistoryEntryResponse:
    return ExecutionHistoryEntryResponse(
        id=entry.id,
        session_id=entry.session_id,
        exec_type=entry.exec_type.value,
        code=entry.code,
        success=entry.success,
        execution_time_ms=entry.execution_time_ms,
        output=entry.output,
        error=entry.error,
        description=entry.description,
        tags=entry.tags,
        notes=entry.notes,
        created_at=entry.created_at,
    )


@router.get("/{sandbox_id}/history", response_model=ExecutionHistoryResponse)
async def get_execution_history(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
    exec_type: str | None = Query(None, pattern="^(python|shell)$"),
    success_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tags: str | None = Query(None),
    has_notes: bool = Query(False),
    has_description: bool = Query(False),
) -> ExecutionHistoryResponse:
    """Get execution history for a sandbox."""
    await sandbox_mgr.get(sandbox_id, owner)

    parsed_exec_type = ExecutionType(exec_type) if exec_type else None
    entries, total = await skill_svc.list_execution_history(
        owner=owner,
        sandbox_id=sandbox_id,
        exec_type=parsed_exec_type,
        success_only=success_only,
        limit=limit,
        offset=offset,
        tags=tags,
        has_notes=has_notes,
        has_description=has_description,
    )

    return ExecutionHistoryResponse(
        entries=[_to_entry_response(entry) for entry in entries],
        total=total,
    )


@router.get("/{sandbox_id}/history/last", response_model=ExecutionHistoryEntryResponse)
async def get_last_execution(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
    exec_type: str | None = Query(None, pattern="^(python|shell)$"),
) -> ExecutionHistoryEntryResponse:
    """Get latest execution record for a sandbox."""
    await sandbox_mgr.get(sandbox_id, owner)
    parsed_exec_type = ExecutionType(exec_type) if exec_type else None
    entry = await skill_svc.get_last_execution(
        owner=owner,
        sandbox_id=sandbox_id,
        exec_type=parsed_exec_type,
    )
    return _to_entry_response(entry)


@router.get("/{sandbox_id}/history/{execution_id}", response_model=ExecutionHistoryEntryResponse)
async def get_execution(
    sandbox_id: str,
    execution_id: str,
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> ExecutionHistoryEntryResponse:
    """Get one execution record by ID."""
    await sandbox_mgr.get(sandbox_id, owner)
    entry = await skill_svc.get_execution(
        owner=owner,
        sandbox_id=sandbox_id,
        execution_id=execution_id,
    )
    return _to_entry_response(entry)


@router.patch("/{sandbox_id}/history/{execution_id}", response_model=ExecutionHistoryEntryResponse)
async def annotate_execution(
    sandbox_id: str,
    execution_id: str,
    request: AnnotateExecutionRequest,
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> ExecutionHistoryEntryResponse:
    """Annotate one execution record."""
    await sandbox_mgr.get(sandbox_id, owner)
    entry = await skill_svc.annotate_execution(
        owner=owner,
        sandbox_id=sandbox_id,
        execution_id=execution_id,
        description=request.description,
        tags=request.tags,
        notes=request.notes,
    )
    return _to_entry_response(entry)
