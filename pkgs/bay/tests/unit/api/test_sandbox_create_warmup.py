"""Unit tests for sandbox create endpoint warmup behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse

from app.api.v1.sandboxes import CreateSandboxRequest, create_sandbox
from app.models.sandbox import Sandbox


@pytest.mark.asyncio
async def test_create_sandbox_schedules_background_warmup_when_created():
    """Fresh create should enqueue exactly one warmup background task."""
    request = CreateSandboxRequest(profile="python-default", ttl=300)
    background_tasks = BackgroundTasks()

    sandbox_mgr = AsyncMock()
    sandbox_mgr.create.return_value = Sandbox(
        id="sandbox-abc123",
        owner="user-1",
        profile_id="python-default",
        cargo_id="cargo-1",
    )

    idempotency_svc = AsyncMock()
    idempotency_svc.check.return_value = None

    resp = await create_sandbox(
        request=request,
        background_tasks=background_tasks,
        sandbox_mgr=sandbox_mgr,
        idempotency_svc=idempotency_svc,
        owner="user-1",
        idempotency_key=None,
    )

    assert resp.id == "sandbox-abc123"
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    assert task.func.__name__ == "_warmup_sandbox_runtime"
    assert task.kwargs == {"sandbox_id": "sandbox-abc123", "owner": "user-1"}


@pytest.mark.asyncio
async def test_create_sandbox_idempotency_cache_does_not_enqueue_warmup():
    """Idempotency cache hit should return cached JSONResponse without warmup task."""
    request = CreateSandboxRequest(profile="python-default", ttl=300)
    background_tasks = BackgroundTasks()

    sandbox_mgr = AsyncMock()

    idempotency_svc = AsyncMock()
    idempotency_svc.check.return_value = SimpleNamespace(
        response={"id": "sandbox-cached", "status": "idle"},
        status_code=201,
    )

    resp = await create_sandbox(
        request=request,
        background_tasks=background_tasks,
        sandbox_mgr=sandbox_mgr,
        idempotency_svc=idempotency_svc,
        owner="user-1",
        idempotency_key="idem-key-1",
    )

    assert isinstance(resp, JSONResponse)
    assert len(background_tasks.tasks) == 0
    sandbox_mgr.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_sandbox_with_idempotency_save_and_enqueue_warmup():
    """Non-cached idempotent create should save key and enqueue warmup task."""
    request = CreateSandboxRequest(profile="python-default", ttl=600)
    background_tasks = BackgroundTasks()

    sandbox_mgr = AsyncMock()
    sandbox_mgr.create.return_value = Sandbox(
        id="sandbox-new-idem",
        owner="user-1",
        profile_id="python-default",
        cargo_id="cargo-2",
    )

    idempotency_svc = AsyncMock()
    idempotency_svc.check.return_value = None

    resp = await create_sandbox(
        request=request,
        background_tasks=background_tasks,
        sandbox_mgr=sandbox_mgr,
        idempotency_svc=idempotency_svc,
        owner="user-1",
        idempotency_key="idem-key-2",
    )

    assert resp.id == "sandbox-new-idem"
    idempotency_svc.save.assert_awaited_once()
    assert len(background_tasks.tasks) == 1
