"""Idempotency-Key support tests.

Purpose: Verify idempotent sandbox creation with Idempotency-Key header.

Parallel-safe: Yes - uses UUID for unique idempotency keys.
"""

from __future__ import annotations

import uuid

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_idempotent_create_returns_same_response():
    """Same Idempotency-Key returns same sandbox on retry."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        idem_key = f"test-idem-{uuid.uuid4()}"
        body = {"profile": DEFAULT_PROFILE}

        # First request
        r1 = await client.post(
            "/v1/sandboxes",
            json=body,
            headers={"Idempotency-Key": idem_key},
        )
        assert r1.status_code == 201
        sandbox1 = r1.json()

        try:
            # Second request with same key - should return cached response
            r2 = await client.post(
                "/v1/sandboxes",
                json=body,
                headers={"Idempotency-Key": idem_key},
            )
            assert r2.status_code == 201
            assert r2.json()["id"] == sandbox1["id"]

        finally:
            await client.delete(f"/v1/sandboxes/{sandbox1['id']}")


async def test_idempotent_create_conflict_on_different_body():
    """Same Idempotency-Key with different body returns 409."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        idem_key = f"test-conflict-{uuid.uuid4()}"

        # First request
        r1 = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
            headers={"Idempotency-Key": idem_key},
        )
        assert r1.status_code == 201
        sandbox_id = r1.json()["id"]

        try:
            # Second request - same key, different body
            r2 = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 3600},
                headers={"Idempotency-Key": idem_key},
            )
            assert r2.status_code == 409
            assert r2.json()["error"]["code"] == "conflict"

        finally:
            await client.delete(f"/v1/sandboxes/{sandbox_id}")


async def test_create_without_idempotency_key_creates_multiple():
    """Create without Idempotency-Key creates separate sandboxes."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        r1 = await client.post("/v1/sandboxes", json={"profile": DEFAULT_PROFILE})
        r2 = await client.post("/v1/sandboxes", json={"profile": DEFAULT_PROFILE})
        assert r1.status_code == 201
        assert r2.status_code == 201

        try:
            assert r1.json()["id"] != r2.json()["id"]
        finally:
            await client.delete(f"/v1/sandboxes/{r1.json()['id']}")
            await client.delete(f"/v1/sandboxes/{r2.json()['id']}")


async def test_invalid_idempotency_key_format_returns_409():
    """Invalid Idempotency-Key format returns 409."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
            headers={"Idempotency-Key": "invalid key with spaces"},
        )
        assert resp.status_code == 409
