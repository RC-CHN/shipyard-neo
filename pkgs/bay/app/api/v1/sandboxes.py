"""Sandboxes API endpoints.

See: plans/bay-api.md section 6.1
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import structlog
from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.adapters.base import BaseAdapter
from app.adapters.gull import GullAdapter
from app.adapters.ship import ShipAdapter
from app.api.dependencies import AuthDep, IdempotencyServiceDep, SandboxManagerDep
from app.config import get_settings
from app.models.sandbox import Sandbox, SandboxStatus
from app.models.session import Session
from app.router.capability.adapter_pool import default_adapter_pool
from app.utils.datetime import utcnow

router = APIRouter()
_log = structlog.get_logger()

# Timeout for container runtime queries (seconds)
_CONTAINER_QUERY_TIMEOUT = 2.0


# Request/Response Models


class CreateSandboxRequest(BaseModel):
    """Request to create a sandbox."""

    profile: str = "python-default"
    cargo_id: str | None = None
    ttl: int | None = None  # seconds, null/0 = no expiry


class ContainerRuntimeResponse(BaseModel):
    """Runtime container status within a sandbox."""

    name: str  # Container name, e.g. "ship", "browser"
    runtime_type: str  # ship | gull
    status: str  # running | stopped | failed
    version: str | None = None  # Runtime version, e.g. "0.1.2"
    capabilities: list[str]  # Capabilities provided by this container
    healthy: bool | None = None  # Health status (None = not checked)


class SandboxResponse(BaseModel):
    """Sandbox response model."""

    id: str
    status: str
    profile: str
    cargo_id: str
    capabilities: list[str]
    created_at: datetime
    expires_at: datetime | None
    idle_expires_at: datetime | None
    containers: list[ContainerRuntimeResponse] | None = None


class SandboxListResponse(BaseModel):
    """Sandbox list response."""

    items: list[SandboxResponse]
    next_cursor: str | None = None


class ExtendTTLRequest(BaseModel):
    """Request to extend sandbox TTL."""

    extend_by: int


def _sandbox_to_response(
    sandbox: Sandbox,
    current_session: Session | None = None,
    *,
    containers: list[ContainerRuntimeResponse] | None = None,
) -> SandboxResponse:
    """Convert Sandbox model to API response."""
    now = utcnow()
    return _sandbox_to_response_at_time(
        sandbox,
        now=now,
        current_session=current_session,
        containers=containers,
    )


def _sandbox_to_response_at_time(
    sandbox,
    *,
    now: datetime,
    current_session=None,
    status: SandboxStatus | None = None,
    containers: list[ContainerRuntimeResponse] | None = None,
) -> SandboxResponse:
    """Convert Sandbox model to API response using a fixed time reference."""
    settings = get_settings()
    profile = settings.get_profile(sandbox.profile_id)

    # Phase 2: multi-container profiles may not set legacy `profile.capabilities`.
    if profile is None:
        capabilities: list[str] = []
    else:
        capabilities = (
            list(profile.capabilities)
            if getattr(profile, "capabilities", None)
            else sorted(profile.get_all_capabilities())
        )

    computed_status = status or sandbox.compute_status(now=now, current_session=current_session)

    return SandboxResponse(
        id=sandbox.id,
        status=computed_status.value,
        profile=sandbox.profile_id,
        cargo_id=sandbox.cargo_id,
        capabilities=capabilities,
        created_at=sandbox.created_at,
        expires_at=sandbox.expires_at,
        idle_expires_at=sandbox.idle_expires_at,
        containers=containers,
    )


def _make_adapter(endpoint: str, runtime_type: str) -> BaseAdapter:
    """Create or retrieve a cached adapter for a container endpoint."""
    pool_key = f"{endpoint}::{runtime_type}"

    def factory() -> BaseAdapter:
        if runtime_type == "ship":
            return ShipAdapter(endpoint)
        if runtime_type == "gull":
            return GullAdapter(endpoint)
        raise ValueError(f"Unknown runtime type: {runtime_type}")

    return default_adapter_pool.get_or_create(pool_key, factory)


async def _query_single_container(
    container: dict,
) -> ContainerRuntimeResponse:
    """Query runtime version and health for a single container.

    Uses cached adapter.get_meta() for version and real-time health() check.
    Falls back gracefully on errors (version=None, healthy=None).
    """
    name = container.get("name", "unknown")
    runtime_type = container.get("runtime_type", "ship")
    status = container.get("status", "unknown")
    capabilities = container.get("capabilities", [])
    endpoint = container.get("endpoint")

    version: str | None = None
    healthy: bool | None = None

    if endpoint and status == "running":
        try:
            adapter = _make_adapter(endpoint, runtime_type)

            # Query meta (cached) and health (real-time) concurrently
            meta_task = asyncio.create_task(adapter.get_meta())
            health_task = asyncio.create_task(adapter.health())

            results = await asyncio.gather(
                meta_task, health_task, return_exceptions=True
            )

            if not isinstance(results[0], BaseException):
                version = results[0].version
            else:
                _log.warning(
                    "container.meta_failed",
                    container=name,
                    error=str(results[0]),
                )

            if not isinstance(results[1], BaseException):
                healthy = results[1]
            else:
                _log.warning(
                    "container.health_failed",
                    container=name,
                    error=str(results[1]),
                )
        except Exception as exc:
            _log.warning(
                "container.query_failed",
                container=name,
                error=str(exc),
            )

    return ContainerRuntimeResponse(
        name=name,
        runtime_type=runtime_type,
        status=status,
        version=version,
        capabilities=capabilities,
        healthy=healthy,
    )


async def _query_containers_status(
    session: Session,
) -> list[ContainerRuntimeResponse] | None:
    """Query runtime status for all containers in a session.

    Returns None if the session has no containers or is not running.
    Uses asyncio.gather with a global timeout to avoid blocking the API.
    """
    if not session.containers:
        # Single-container (legacy) fallback: build from primary session fields
        if session.endpoint and session.runtime_type:
            containers_data = [
                {
                    "name": session.runtime_type,
                    "runtime_type": session.runtime_type,
                    "status": "running" if session.is_ready else "unknown",
                    "endpoint": session.endpoint,
                    "capabilities": [],  # Will be filled from meta
                }
            ]
        else:
            return None
    else:
        containers_data = session.containers

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *[_query_single_container(c) for c in containers_data],
                return_exceptions=True,
            ),
            timeout=_CONTAINER_QUERY_TIMEOUT,
        )

        container_responses: list[ContainerRuntimeResponse] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                # Build a degraded response for failed queries
                c = containers_data[i]
                container_responses.append(
                    ContainerRuntimeResponse(
                        name=c.get("name", "unknown"),
                        runtime_type=c.get("runtime_type", "ship"),
                        status=c.get("status", "unknown"),
                        capabilities=c.get("capabilities", []),
                    )
                )
            else:
                container_responses.append(result)

        return container_responses

    except TimeoutError:
        _log.warning("containers.query_timeout", sandbox_id=session.sandbox_id)
        # Return basic info without version/health on timeout
        return [
            ContainerRuntimeResponse(
                name=c.get("name", "unknown"),
                runtime_type=c.get("runtime_type", "ship"),
                status=c.get("status", "unknown"),
                capabilities=c.get("capabilities", []),
            )
            for c in containers_data
        ]


# Endpoints


@router.post("", response_model=SandboxResponse, status_code=201)
async def create_sandbox(
    request: CreateSandboxRequest,
    sandbox_mgr: SandboxManagerDep,
    idempotency_svc: IdempotencyServiceDep,
    owner: AuthDep,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> SandboxResponse | JSONResponse:
    """Create a new sandbox.

    - Lazy session creation: status may be 'idle' initially
    - ttl=null or ttl=0 means no expiry
    - Supports Idempotency-Key header for safe retries
    """
    # Serialize request body for fingerprinting
    request_body = request.model_dump_json()
    request_path = "/v1/sandboxes"

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

    # 2. Create sandbox
    sandbox = await sandbox_mgr.create(
        owner=owner,
        profile_id=request.profile,
        cargo_id=request.cargo_id,
        ttl=request.ttl,
    )
    response = _sandbox_to_response(sandbox)

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


@router.get("", response_model=SandboxListResponse)
async def list_sandboxes(
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    status: str | None = Query(None),
) -> SandboxListResponse:
    """List sandboxes for the current user."""
    # Convert string status to enum if provided
    status_filter = None
    if status:
        try:
            status_filter = SandboxStatus(status)
        except ValueError:
            pass  # Invalid status, ignore filter

    sandboxes, next_cursor = await sandbox_mgr.list(
        owner=owner,
        status=status_filter,
        limit=limit,
        cursor=cursor,
    )

    now = utcnow()
    items = [
        _sandbox_to_response_at_time(
            item.sandbox,
            now=now,
            status=item.status,
        )
        for item in sandboxes
    ]
    return SandboxListResponse(items=items, next_cursor=next_cursor)


@router.get("/{sandbox_id}", response_model=SandboxResponse)
async def get_sandbox(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> SandboxResponse:
    """Get sandbox details.

    When the sandbox has an active session, the response includes a
    ``containers`` list with each container's runtime version and health
    status, queried in real-time from the container endpoints.
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    current_session = await sandbox_mgr.get_current_session(sandbox)

    # Query container runtime status when session is running
    containers = None
    if current_session and current_session.is_ready:
        containers = await _query_containers_status(current_session)

    return _sandbox_to_response(sandbox, current_session, containers=containers)


@router.post(
    "/{sandbox_id}/extend_ttl",
    response_model=SandboxResponse,
    status_code=200,
)
async def extend_ttl(
    sandbox_id: str,
    request: ExtendTTLRequest,
    sandbox_mgr: SandboxManagerDep,
    idempotency_svc: IdempotencyServiceDep,
    owner: AuthDep,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> SandboxResponse | JSONResponse:
    """Extend sandbox TTL (expires_at) by N seconds.

    - Does not resurrect expired sandboxes
    - Does not apply to infinite TTL sandboxes
    - Supports Idempotency-Key for safe retries
    """
    request_body = request.model_dump_json()
    request_path = f"/v1/sandboxes/{sandbox_id}/extend_ttl"

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
            return JSONResponse(
                content=cached.response,
                status_code=cached.status_code,
            )

    # 2. Execute business logic
    sandbox = await sandbox_mgr.extend_ttl(
        sandbox_id=sandbox_id,
        owner=owner,
        extend_by=request.extend_by,
    )
    response = _sandbox_to_response(sandbox)

    # 3. Save idempotency key if provided
    if idempotency_key:
        await idempotency_svc.save(
            owner=owner,
            key=idempotency_key,
            path=request_path,
            method="POST",
            body=request_body,
            response=response,
            status_code=200,
        )

    return response


@router.post("/{sandbox_id}/keepalive", status_code=200)
async def keepalive(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> dict[str, str]:
    """Keep sandbox alive - extends idle timeout only, not TTL.

    Does not implicitly start compute if no session exists.
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    await sandbox_mgr.keepalive(sandbox)
    return {"status": "ok"}


@router.post("/{sandbox_id}/stop", status_code=200)
async def stop_sandbox(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> dict[str, str]:
    """Stop sandbox - reclaims compute, keeps workspace.

    Idempotent: repeated calls maintain final state consistency.
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    await sandbox_mgr.stop(sandbox)
    return {"status": "stopped"}


@router.delete("/{sandbox_id}", status_code=204)
async def delete_sandbox(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> None:
    """Delete sandbox permanently.

    - Destroys all running sessions
    - Cascade deletes managed cargo
    - Does NOT cascade delete external cargo
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    await sandbox_mgr.delete(sandbox)
