"""Cargo API tests: CRUD operations and lifecycle.

Purpose: Verify /v1/cargos API endpoints work correctly.

Test cases from: plans/phase-1.5/cargo-api-implementation.md section 7 (ported to Cargo)

Parallel-safe: Yes - each test creates/deletes its own resources.

Note: GC-related tests are in tests/integration/gc/test_cargo_gc.py
to ensure serial execution.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import pytest

from ..conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    CLEANUP_TIMEOUT,
    DEFAULT_PROFILE,
    DEFAULT_TIMEOUT,
    create_sandbox,
    docker_volume_exists,
    e2e_skipif_marks,
)

pytestmark = e2e_skipif_marks


# =============================================================================
# HELPERS
# =============================================================================


@asynccontextmanager
async def create_cargo(
    client: httpx.AsyncClient,
    *,
    size_limit_mb: int | None = None,
    idempotency_key: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Create external cargo with auto-cleanup."""
    body = {}
    if size_limit_mb is not None:
        body["size_limit_mb"] = size_limit_mb

    headers = {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    resp = await client.post(
        "/v1/cargos", json=body, headers=headers, timeout=DEFAULT_TIMEOUT
    )
    assert resp.status_code == 201, f"Create cargo failed: {resp.text}"
    cargo = resp.json()

    try:
        yield cargo
    finally:
        try:
            await client.delete(
                f"/v1/cargos/{cargo['id']}",
                timeout=CLEANUP_TIMEOUT,
            )
        except httpx.TimeoutException:
            import warnings

            warnings.warn(
                f"Timeout deleting cargo {cargo['id']} during cleanup.",
                stacklevel=2,
            )
        except httpx.HTTPStatusError:
            # 409 during cleanup is expected if still referenced - ignore
            pass


# =============================================================================
# CREATE WORKSPACE TESTS
# =============================================================================


async def test_create_cargo_returns_valid_response():
    """Create external cargo returns required fields with correct format."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_cargo(client) as cargo:
            assert cargo["id"].startswith("ws-")
            assert cargo["managed"] is False
            assert cargo["managed_by_sandbox_id"] is None
            assert cargo["backend"] == "docker_volume"
            assert "size_limit_mb" in cargo
            assert "created_at" in cargo
            assert "last_accessed_at" in cargo


async def test_create_cargo_with_custom_size():
    """Create cargo with custom size_limit_mb."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_cargo(client, size_limit_mb=2048) as cargo:
            assert cargo["size_limit_mb"] == 2048


async def test_create_cargo_idempotency():
    """Create cargo with Idempotency-Key returns same result on retry (D4)."""
    import uuid

    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Use UUID to ensure uniqueness across parallel test runs
        idempotency_key = f"test-cargo-idem-{uuid.uuid4().hex}"

        # First request
        resp1 = await client.post(
            "/v1/cargos",
            json={"size_limit_mb": 1024},
            headers={"Idempotency-Key": idempotency_key},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp1.status_code == 201, f"First request failed: {resp1.text}"
        cargo1 = resp1.json()

        # Second request with same key
        resp2 = await client.post(
            "/v1/cargos",
            json={"size_limit_mb": 1024},
            headers={"Idempotency-Key": idempotency_key},
            timeout=DEFAULT_TIMEOUT,
        )
        # Should return cached response (could be 200 or 201 depending on implementation)
        assert resp2.status_code in (200, 201), f"Second request failed: {resp2.text}"
        cargo2 = resp2.json()

        assert cargo1["id"] == cargo2["id"]

        # Cleanup
        await client.delete(
            f"/v1/cargos/{cargo1['id']}", timeout=CLEANUP_TIMEOUT
        )


async def test_create_cargo_size_limit_validation():
    """size_limit_mb must be in range 1-65536 (D5)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Too small
        resp = await client.post(
            "/v1/cargos",
            json={"size_limit_mb": 0},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 422  # Pydantic validation error

        # Too large
        resp = await client.post(
            "/v1/cargos",
            json={"size_limit_mb": 100000},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 422


# =============================================================================
# LIST WORKSPACE TESTS
# =============================================================================


async def test_list_cargos_default_returns_external_only():
    """GET /v1/cargos defaults to external cargos only (D1)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create an external cargo
        async with create_cargo(client) as ext_cargo:
            # Create a sandbox (creates managed cargo)
            async with create_sandbox(client) as sandbox:
                # List without managed param - should only show external
                resp = await client.get("/v1/cargos", timeout=DEFAULT_TIMEOUT)
                assert resp.status_code == 200
                data = resp.json()

                cargo_ids = [c["id"] for c in data["items"]]
                assert ext_cargo["id"] in cargo_ids

                # Managed cargo should NOT be in default list
                for item in data["items"]:
                    assert item["managed"] is False


async def test_list_cargos_managed_filter():
    """GET /v1/cargos?managed=true shows managed cargos (D1)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            managed_cargo_id = sandbox["cargo_id"]

            # List with managed=true
            resp = await client.get(
                "/v1/cargos?managed=true", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 200
            data = resp.json()

            cargo_ids = [c["id"] for c in data["items"]]
            assert managed_cargo_id in cargo_ids

            # All items should be managed
            for item in data["items"]:
                assert item["managed"] is True


async def test_list_cargos_pagination():
    """List cargos supports pagination."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create multiple cargos
        cargos = []
        for _ in range(3):
            resp = await client.post(
                "/v1/cargos", json={}, timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 201
            cargos.append(resp.json())

        try:
            # List with limit=2
            resp = await client.get(
                "/v1/cargos?limit=2", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 200
            data = resp.json()

            # Should have 2 items and a cursor (if there are more)
            assert len(data["items"]) <= 2

        finally:
            # Cleanup
            for cargo in cargos:
                await client.delete(
                    f"/v1/cargos/{cargo['id']}", timeout=CLEANUP_TIMEOUT
                )


# =============================================================================
# GET CARGO TESTS
# =============================================================================


async def test_get_cargo_returns_details():
    """GET /v1/cargos/{id} returns cargo details."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_cargo(client, size_limit_mb=512) as cargo:
            resp = await client.get(
                f"/v1/cargos/{cargo['id']}", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["id"] == cargo["id"]
            assert data["managed"] is False
            assert data["size_limit_mb"] == 512


async def test_get_cargo_not_found():
    """GET /v1/cargos/{id} returns 404 for non-existent cargo."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        resp = await client.get(
            "/v1/cargos/ws-nonexistent", timeout=DEFAULT_TIMEOUT
        )
        assert resp.status_code == 404


# =============================================================================
# DELETE CARGO TESTS - External Cargo
# =============================================================================


async def test_delete_external_cargo_success():
    """DELETE /v1/cargos/{id} succeeds for unreferenced external cargo."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create cargo (not using context manager)
        resp = await client.post(
            "/v1/cargos", json={}, timeout=DEFAULT_TIMEOUT
        )
        assert resp.status_code == 201
        cargo = resp.json()
        cargo_id = cargo["id"]
        volume_name = f"bay-cargo-{cargo_id}"

        # Verify volume exists
        assert docker_volume_exists(volume_name)

        # Delete
        resp = await client.delete(
            f"/v1/cargos/{cargo_id}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204

        # Verify gone
        await asyncio.sleep(0.5)
        assert not docker_volume_exists(volume_name)


async def test_delete_external_cargo_referenced_by_active_sandbox():
    """DELETE /v1/cargos/{id} returns 409 when referenced by active sandbox (D3)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create external cargo
        async with create_cargo(client) as cargo:
            cargo_id = cargo["id"]

            # Create sandbox using this cargo
            resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "cargo_id": cargo_id},
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 201
            sandbox = resp.json()

            try:
                # Try to delete cargo - should fail with 409
                resp = await client.delete(
                    f"/v1/cargos/{cargo_id}", timeout=DEFAULT_TIMEOUT
                )
                assert resp.status_code == 409

                # Verify error has active_sandbox_ids
                error = resp.json()
                assert "active_sandbox_ids" in error.get("error", {}).get(
                    "details", {}
                )
                assert sandbox["id"] in error["error"]["details"]["active_sandbox_ids"]

            finally:
                # Cleanup sandbox
                await client.delete(
                    f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
                )


async def test_delete_external_cargo_after_sandbox_deleted():
    """DELETE /v1/cargos/{id} succeeds after referencing sandbox is deleted."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        # Create external cargo
        resp = await client.post(
            "/v1/cargos", json={}, timeout=DEFAULT_TIMEOUT
        )
        assert resp.status_code == 201
        cargo = resp.json()
        cargo_id = cargo["id"]

        # Create sandbox using this cargo
        resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE, "cargo_id": cargo_id},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 201
        sandbox = resp.json()

        # Delete sandbox
        resp = await client.delete(
            f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204

        # Now cargo can be deleted
        resp = await client.delete(
            f"/v1/cargos/{cargo_id}", timeout=CLEANUP_TIMEOUT
        )
        assert resp.status_code == 204


# =============================================================================
# DELETE CARGO TESTS - Managed Cargo (D2)
# =============================================================================


async def test_delete_managed_cargo_active_sandbox_returns_409():
    """DELETE /v1/cargos/{id} returns 409 for managed cargo with active sandbox."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_sandbox(client) as sandbox:
            managed_cargo_id = sandbox["cargo_id"]

            # Try to delete managed cargo - should fail
            resp = await client.delete(
                f"/v1/cargos/{managed_cargo_id}", timeout=DEFAULT_TIMEOUT
            )
            assert resp.status_code == 409


# Note: test_delete_managed_workspace_after_sandbox_soft_deleted is covered
# in unit tests (test_cargo_manager.py) because in the real API flow,
# sandbox delete cascade-deletes the managed workspace, so it won't exist
# for a subsequent API delete call. The D2 decision scenario (orphan workspace
# after sandbox soft-delete) is properly tested at the unit test level.


# =============================================================================
# SANDBOX + CARGO INTEGRATION TESTS
# =============================================================================


async def test_sandbox_with_external_cargo():
    """Create sandbox binding external cargo works correctly."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS
    ) as client:
        async with create_cargo(client) as cargo:
            # Create sandbox with this cargo
            resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "cargo_id": cargo["id"]},
                timeout=DEFAULT_TIMEOUT,
            )
            assert resp.status_code == 201
            sandbox = resp.json()

            try:
                assert sandbox["cargo_id"] == cargo["id"]

                # Execute some code to verify cargo works
                exec_resp = await client.post(
                    f"/v1/sandboxes/{sandbox['id']}/python/exec",
                    json={"code": "print('hello')", "timeout": 30},
                    timeout=120.0,
                )
                assert exec_resp.status_code == 200

            finally:
                await client.delete(
                    f"/v1/sandboxes/{sandbox['id']}", timeout=CLEANUP_TIMEOUT
                )
