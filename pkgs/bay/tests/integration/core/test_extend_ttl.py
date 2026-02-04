"""TTL extension tests.

Purpose: Verify POST /v1/sandboxes/{id}/extend_ttl semantics.

Parallel-safe: Most tests are parallel-safe. Exception:
- test_extend_ttl_rejects_expired: Marked with xdist_group("gc") for serial execution
  due to TTL expiration timing sensitivity.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_extend_ttl_success():
    """extend_ttl extends expires_at timestamp."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Create with TTL
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE, "ttl": 3600},
        )
        assert create_resp.status_code == 201
        sandbox = create_resp.json()
        sandbox_id = sandbox["id"]

        try:
            old_expires_at = sandbox["expires_at"]
            assert old_expires_at is not None

            # Extend
            extend_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                json={"extend_by": 600},
            )
            assert extend_resp.status_code == 200
            updated = extend_resp.json()
            assert updated["id"] == sandbox_id
            assert updated["expires_at"] != old_expires_at

        finally:
            await client.delete(f"/v1/sandboxes/{sandbox_id}")


async def test_extend_ttl_idempotency():
    """Same Idempotency-Key returns identical cached response."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE, "ttl": 3600},
        )
        assert create_resp.status_code == 201
        sandbox_id = create_resp.json()["id"]

        try:
            idem_key = f"test-extend-{uuid.uuid4()}"
            body = {"extend_by": 123}

            r1 = await client.post(
                f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                json=body,
                headers={"Idempotency-Key": idem_key},
            )
            assert r1.status_code == 200
            j1 = r1.json()

            time.sleep(0.05)

            r2 = await client.post(
                f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                json=body,
                headers={"Idempotency-Key": idem_key},
            )
            assert r2.status_code == 200
            assert r2.json() == j1

        finally:
            await client.delete(f"/v1/sandboxes/{sandbox_id}")


async def test_extend_ttl_rejects_infinite_ttl():
    """extend_ttl on infinite-TTL sandbox returns 409 sandbox_ttl_infinite."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # No TTL = infinite
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE},
        )
        assert create_resp.status_code == 201
        sandbox_id = create_resp.json()["id"]

        try:
            extend_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                json={"extend_by": 10},
            )
            assert extend_resp.status_code == 409
            assert extend_resp.json()["error"]["code"] == "sandbox_ttl_infinite"

        finally:
            await client.delete(f"/v1/sandboxes/{sandbox_id}")


async def test_extend_ttl_rejects_expired():
    """extend_ttl on expired sandbox returns 404 or 409 sandbox_expired.

    This test is marked serial via conftest.py SERIAL_TESTS["gc"] pattern.
    After TTL expires, extend_ttl should either:
    - 409 sandbox_expired: sandbox exists but expired
    - 404 not_found: GC already deleted it
    """
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # Short TTL
        create_resp = await client.post(
            "/v1/sandboxes",
            json={"profile": DEFAULT_PROFILE, "ttl": 2},
        )
        assert create_resp.status_code == 201
        sandbox_id = create_resp.json()["id"]

        try:
            # Wait for expiry (extra margin for clock drift between client/server)
            await asyncio.sleep(5)

            extend_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                json={"extend_by": 10},
            )
            assert extend_resp.status_code in (404, 409), \
                f"Expected 404/409, got {extend_resp.status_code}: {extend_resp.text}"

            if extend_resp.status_code == 409:
                assert extend_resp.json()["error"]["code"] == "sandbox_expired"

        finally:
            # May already be deleted by GC
            await client.delete(f"/v1/sandboxes/{sandbox_id}")
