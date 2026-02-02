"""E2E-XX: Sandbox TTL extension (extend_ttl) tests.

Purpose: Verify POST /v1/sandboxes/{id}/extend_ttl semantics:
- Success extends expires_at
- Idempotency-Key replays same response
- Reject expired sandbox (409 sandbox_expired)
- Reject infinite TTL sandbox (409 sandbox_ttl_infinite)
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestE2EExtendTTL:
    """E2E: extend_ttl endpoint."""

    async def test_extend_ttl_success(self):
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox with TTL so expires_at is set
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

                # extend
                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 600},
                )
                assert extend_resp.status_code == 200
                updated = extend_resp.json()
                assert updated["id"] == sandbox_id
                assert updated["expires_at"] is not None
                assert updated["expires_at"] != old_expires_at
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_extend_ttl_idempotency_replays_same_response(self):
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 3600},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                idem_key = f"test-extend-ttl-{uuid.uuid4()}"
                body = {"extend_by": 123}

                r1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json=body,
                    headers={"Idempotency-Key": idem_key},
                )
                assert r1.status_code == 200
                j1 = r1.json()

                # slight delay; replay should still return identical snapshot
                time.sleep(0.05)

                r2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json=body,
                    headers={"Idempotency-Key": idem_key},
                )
                assert r2.status_code == 200
                j2 = r2.json()

                assert j2 == j1
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_extend_ttl_rejects_infinite_ttl(self):
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # ttl omitted -> infinite
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
                payload = extend_resp.json()
                assert payload["error"]["code"] == "sandbox_ttl_infinite"
            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_extend_ttl_rejects_expired(self):
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create with short TTL (3s gives enough margin)
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 3},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                # wait for expiry (3s TTL + small buffer)
                # Note: GC interval is 5s, so sandbox won't be deleted yet
                time.sleep(3.5)

                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 10},
                )
                assert extend_resp.status_code == 409, \
                    f"Expected 409 sandbox_expired, got {extend_resp.status_code}: {extend_resp.text}"
                payload = extend_resp.json()
                assert payload["error"]["code"] == "sandbox_expired"
            finally:
                # May already be deleted by GC, ignore errors
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
