"""Workspaces API endpoints.

See: plans/phase-1.5/workspace-api-implementation.md
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.dependencies import (
    AuthDep,
    IdempotencyServiceDep,
    WorkspaceManagerDep,
)
from app.errors import ValidationError

router = APIRouter()


# Request/Response Models


class CreateWorkspaceRequest(BaseModel):
    """Request to create an external workspace."""

    size_limit_mb: int | None = Field(
        default=None,
        ge=1,
        le=65536,
        description="Size limit in MB (1-65536). If null, uses default.",
    )


class WorkspaceResponse(BaseModel):
    """Workspace response model.
    
    Note: owner field is intentionally not exposed per API design.
    """

    id: str
    managed: bool
    managed_by_sandbox_id: str | None
    backend: str
    size_limit_mb: int
    created_at: datetime
    last_accessed_at: datetime


class WorkspaceListResponse(BaseModel):
    """Workspace list response."""

    items: list[WorkspaceResponse]
    next_cursor: str | None = None


def _workspace_to_response(workspace) -> WorkspaceResponse:
    """Convert Workspace model to API response."""
    return WorkspaceResponse(
        id=workspace.id,
        managed=workspace.managed,
        managed_by_sandbox_id=workspace.managed_by_sandbox_id,
        backend=workspace.backend,
        size_limit_mb=workspace.size_limit_mb,
        created_at=workspace.created_at,
        last_accessed_at=workspace.last_accessed_at,
    )


# Endpoints


@router.post("", response_model=WorkspaceResponse, status_code=201)
async def create_workspace(
    request: CreateWorkspaceRequest,
    workspace_mgr: WorkspaceManagerDep,
    idempotency_svc: IdempotencyServiceDep,
    owner: AuthDep,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> WorkspaceResponse | JSONResponse:
    """Create a new external workspace.
    
    External workspaces:
    - Are not managed by any sandbox
    - Must be explicitly deleted by the user
    - Can be shared across multiple sandboxes
    - Supports Idempotency-Key header for safe retries
    """
    # Validate size_limit_mb if provided (Pydantic handles range, but extra safety)
    if request.size_limit_mb is not None:
        if not isinstance(request.size_limit_mb, int):
            raise ValidationError(
                "size_limit_mb must be an integer",
                details={"size_limit_mb": request.size_limit_mb},
            )

    # Serialize request body for fingerprinting
    request_body = request.model_dump_json()
    request_path = "/v1/workspaces"
    
    # 1. Check idempotency key if provided
    if idempotency_key:
        cached = await idempotency_svc.check(
            owner=owner,
            key=idempotency_key,
            path=request_path,
            method="POST",
            body=request_body,
        )
        if cached:
            # Return cached response with original status code
            return JSONResponse(
                content=cached.response,
                status_code=cached.status_code,
            )
    
    # 2. Create external workspace (managed=False)
    workspace = await workspace_mgr.create(
        owner=owner,
        managed=False,  # External workspace
        managed_by_sandbox_id=None,
        size_limit_mb=request.size_limit_mb,
    )
    response = _workspace_to_response(workspace)
    
    # 3. Save idempotency key if provided
    if idempotency_key:
        await idempotency_svc.save(
            owner=owner,
            key=idempotency_key,
            path=request_path,
            method="POST",
            body=request_body,
            response=response,
            status_code=201,
        )

    return response


@router.get("", response_model=WorkspaceListResponse)
async def list_workspaces(
    workspace_mgr: WorkspaceManagerDep,
    owner: AuthDep,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    managed: bool | None = Query(
        None,
        description="Filter by managed status. Default (null/omitted) returns only external workspaces (managed=false). "
        "Set to true to see managed workspaces only.",
    ),
) -> WorkspaceListResponse:
    """List workspaces for the current user.
    
    By default (D1 decision), only external workspaces (managed=false) are returned.
    Pass managed=true to see managed workspaces instead.
    """
    # D1 decision: default to showing only external workspaces (managed=False)
    # If managed is not provided (None), use False as default
    effective_managed = managed if managed is not None else False
    
    workspaces, next_cursor = await workspace_mgr.list(
        owner=owner,
        managed=effective_managed,
        limit=limit,
        cursor=cursor,
    )

    items = [_workspace_to_response(w) for w in workspaces]
    return WorkspaceListResponse(items=items, next_cursor=next_cursor)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    workspace_mgr: WorkspaceManagerDep,
    owner: AuthDep,
) -> WorkspaceResponse:
    """Get workspace details."""
    workspace = await workspace_mgr.get(workspace_id, owner)
    return _workspace_to_response(workspace)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    workspace_mgr: WorkspaceManagerDep,
    owner: AuthDep,
) -> None:
    """Delete a workspace.
    
    For external workspaces:
    - Cannot delete if still referenced by active sandboxes
    - Returns 409 with active_sandbox_ids if in use
    
    For managed workspaces:
    - Can delete if the managing sandbox is soft-deleted
    - Returns 409 if managing sandbox is still active
    """
    await workspace_mgr.delete(workspace_id, owner, force=False)
