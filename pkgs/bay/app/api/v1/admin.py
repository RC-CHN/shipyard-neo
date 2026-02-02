"""Admin API endpoints.

Provides administrative operations like manual GC trigger.
These endpoints are protected by authentication.

See: plans/phase-1.5/admin-gc-api-design.md
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import AuthDep
from app.services.gc.lifecycle import get_gc_scheduler

# Prefix is applied by the parent v1 router for consistency.
router = APIRouter()


# ---- Request/Response Models ----


class GCRunRequest(BaseModel):
    """Request body for manual GC trigger."""

    tasks: list[str] | None = Field(
        default=None,
        description="List of task names to run. None = all enabled tasks. "
        "Valid names: idle_session, expired_sandbox, orphan_cargo, orphan_container",
    )


class GCTaskResult(BaseModel):
    """Result of a single GC task."""

    task_name: str
    cleaned_count: int
    skipped_count: int
    errors: list[str]


class GCRunResponse(BaseModel):
    """Response from manual GC run."""

    results: list[GCTaskResult]
    total_cleaned: int
    total_errors: int
    duration_ms: int


class GCStatusResponse(BaseModel):
    """Response from GC status query."""

    enabled: bool
    is_running: bool
    instance_id: str
    interval_seconds: int
    tasks: dict[str, dict[str, bool]]


# ---- Endpoints ----


@router.post("/gc/run", response_model=GCRunResponse)
async def run_gc(
    owner: AuthDep,
    request: GCRunRequest | None = None,
) -> GCRunResponse:
    """Manually trigger a GC cycle.

    This endpoint runs GC **synchronously** and waits for completion.
    Returns detailed results for each GC task.

    Use this in tests instead of relying on automatic GC timing:
    1. Set `gc.enabled: false` to disable background GC
    2. Call `POST /admin/gc/run` when you want GC to execute

    **Note**: This endpoint works even when `gc.enabled: false`.
    The scheduler is always created to support manual triggering.

    **Authentication**: Requires valid API key (same as other endpoints).

    **Status Codes**:
    - 200: GC executed successfully (even if some items had errors)
    - 423: GC is already running (another cycle in progress)
    - 503: GC scheduler unavailable (unexpected internal error)
    """
    scheduler = get_gc_scheduler()

    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="GC scheduler is not available. This is unexpected - please check server logs.",
        )

    # Check if already running (non-blocking)
    # Access internal lock to avoid waiting
    if scheduler._run_lock.locked():
        raise HTTPException(
            status_code=423,
            detail="GC is already running. Please wait for the current cycle to complete.",
        )

    start = time.monotonic()

    # Run GC cycle
    # If request.tasks is provided, run only those tasks (in scheduler order).
    # Unknown task names are ignored.
    results = await scheduler.run_once()

    if request and request.tasks is not None:
        allowed = set(request.tasks)
        results = [r for r in results if (r.task_name or "") in allowed]

    duration_ms = int((time.monotonic() - start) * 1000)

    return GCRunResponse(
        results=[
            GCTaskResult(
                task_name=r.task_name or "unknown",
                cleaned_count=r.cleaned_count,
                skipped_count=r.skipped_count,
                errors=r.errors,
            )
            for r in results
        ],
        total_cleaned=sum(r.cleaned_count for r in results),
        total_errors=sum(len(r.errors) for r in results),
        duration_ms=duration_ms,
    )


@router.get("/gc/status", response_model=GCStatusResponse)
async def get_gc_status(
    owner: AuthDep,
) -> GCStatusResponse:
    """Get GC scheduler status.

    Returns current configuration and running state.
    Useful for debugging and monitoring.

    **Authentication**: Requires valid API key (same as other endpoints).
    """
    from app.config import get_settings

    settings = get_settings()
    gc_config = settings.gc
    scheduler = get_gc_scheduler()

    return GCStatusResponse(
        enabled=gc_config.enabled,
        is_running=scheduler._run_lock.locked() if scheduler else False,
        instance_id=gc_config.get_instance_id(),
        interval_seconds=gc_config.interval_seconds,
        tasks={
            "idle_session": {"enabled": gc_config.idle_session.enabled},
            "expired_sandbox": {"enabled": gc_config.expired_sandbox.enabled},
            "orphan_cargo": {"enabled": gc_config.orphan_cargo.enabled},
            "orphan_container": {"enabled": gc_config.orphan_container.enabled},
        },
    )
