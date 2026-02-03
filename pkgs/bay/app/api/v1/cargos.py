"""Cargos API endpoints.

Ported from the Workspace CRUD API introduced on `main`, but aligned to the
Cargo naming used across the Bay codebase.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.dependencies import (
    AuthDep,
    CargoManagerDep,
    IdempotencyServiceDep,
)
from app.errors import ValidationError

router = APIRouter()


# Request/Response Models


class CreateCargoRequest(BaseModel):
    """Request to create an external cargo."""

    size_limit_mb: int | None = Field(
        default=None,
        ge=1,
        le=65536,
        description="Size limit in MB (1-65536). If null, uses default.",
    )


class CargoResponse(BaseModel):
    """Cargo response model.

    Note: owner field is intentionally not exposed per API design.
    """

    id: str
    managed: bool
    managed_by_sandbox_id: str | None
    backend: str
    size_limit_mb: int
    created_at: datetime
    last_accessed_at: datetime


class CargoListResponse(BaseModel):
    """Cargo list response."""

    items: list[CargoResponse]
    next_cursor: str | None = None


def _cargo_to_response(cargo) -> CargoResponse:
    """Convert Cargo model to API response."""
    return CargoResponse(
        id=cargo.id,
        managed=cargo.managed,
        managed_by_sandbox_id=cargo.managed_by_sandbox_id,
        backend=cargo.backend,
        size_limit_mb=cargo.size_limit_mb,
        created_at=cargo.created_at,
        last_accessed_at=cargo.last_accessed_at,
    )


# Endpoints


@router.post("", response_model=CargoResponse, status_code=201)
async def create_cargo(
    request: CreateCargoRequest,
    cargo_mgr: CargoManagerDep,
    idempotency_svc: IdempotencyServiceDep,
    owner: AuthDep,
    http_request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> CargoResponse | JSONResponse:
    """Create a new external cargo.

    External cargos:
    - Are not managed by any sandbox
    - Must be explicitly deleted by the user
    - Can be shared across multiple sandboxes
    - Supports Idempotency-Key header for safe retries
    """
    # Validate size_limit_mb if provided (Pydantic handles range, but extra safety)
    if request.size_limit_mb is not None and not isinstance(request.size_limit_mb, int):
        raise ValidationError(
            "size_limit_mb must be an integer",
            details={"size_limit_mb": request.size_limit_mb},
        )

    # Serialize request body for fingerprinting
    request_body = request.model_dump_json()
    request_path = http_request.url.path

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

    # 2. Create external cargo (managed=False)
    cargo = await cargo_mgr.create(
        owner=owner,
        managed=False,  # External cargo
        managed_by_sandbox_id=None,
        size_limit_mb=request.size_limit_mb,
    )
    response = _cargo_to_response(cargo)

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


@router.get("", response_model=CargoListResponse)
async def list_cargos(
    cargo_mgr: CargoManagerDep,
    owner: AuthDep,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    managed: bool | None = Query(
        None,
        description="Filter by managed status. Default (null/omitted) returns only external cargos (managed=false). "
        "Set to true to see managed cargos only.",
    ),
) -> CargoListResponse:
    """List cargos for the current user.

    By default (D1 decision), only external cargos (managed=false) are returned.
    Pass managed=true to see managed cargos instead.
    """
    # D1 decision: default to showing only external cargos (managed=False)
    # If managed is not provided (None), use False as default
    effective_managed = managed if managed is not None else False

    cargos, next_cursor = await cargo_mgr.list(
        owner=owner,
        managed=effective_managed,
        limit=limit,
        cursor=cursor,
    )

    items = [_cargo_to_response(c) for c in cargos]
    return CargoListResponse(items=items, next_cursor=next_cursor)


@router.get("/{cargo_id}", response_model=CargoResponse)
async def get_cargo(
    cargo_id: str,
    cargo_mgr: CargoManagerDep,
    owner: AuthDep,
) -> CargoResponse:
    """Get cargo details."""
    cargo = await cargo_mgr.get(cargo_id, owner)
    return _cargo_to_response(cargo)


@router.delete("/{cargo_id}", status_code=204)
async def delete_cargo(
    cargo_id: str,
    cargo_mgr: CargoManagerDep,
    owner: AuthDep,
) -> None:
    """Delete a cargo.

    For external cargos:
    - Cannot delete if still referenced by active sandboxes
    - Returns 409 with active_sandbox_ids if in use

    For managed cargos:
    - Can delete if the managing sandbox is soft-deleted
    - Returns 409 if managing sandbox is still active
    """
    await cargo_mgr.delete(cargo_id, owner, force=False)
