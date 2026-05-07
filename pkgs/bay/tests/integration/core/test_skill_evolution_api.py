"""Integration tests for skill evolution API endpoints (Phase 1).

Covers the three new intent-driven endpoints end-to-end against a live Bay:
- POST /v1/skills/goals
- GET  /v1/skills/{skill_key}/active
- POST /v1/skills/outcomes

Prerequisites: Docker, ship:latest, Bay running.
"""

from __future__ import annotations

import httpx
import pytest

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, e2e_skipif_marks

pytestmark = e2e_skipif_marks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_or_create_execution_id(client: httpx.AsyncClient) -> str:
    """Create a minimal sandbox, run a shell command, return the execution_id, then delete it."""
    sandbox_resp = await client.post(
        "/v1/sandboxes",
        json={"profile": "python-default", "ttl": 60},
        timeout=30.0,
    )
    if sandbox_resp.status_code != 201:
        pytest.skip(f"Cannot create sandbox for execution seed (status {sandbox_resp.status_code})")
    sandbox_id = sandbox_resp.json()["id"]

    try:
        exec_resp = await client.post(
            f"/v1/sandboxes/{sandbox_id}/shell/exec",
            json={"command": "echo seed"},
            timeout=30.0,
        )
        if exec_resp.status_code != 200:
            pytest.skip(
                f"Cannot run shell command for execution seed (status {exec_resp.status_code})"
            )
        return exec_resp.json()["execution_id"]
    finally:
        await client.delete(f"/v1/sandboxes/{sandbox_id}", timeout=15.0)


async def _create_candidate_and_promote(
    client: httpx.AsyncClient,
    *,
    skill_key: str,
    summary: str = "Test skill summary",
) -> tuple[str, str]:
    """Create a candidate, evaluate it, promote to canary. Returns (candidate_id, release_id)."""
    execution_id = await _get_or_create_execution_id(client)

    payload_resp = await client.post(
        "/v1/skills/payloads",
        json={"payload": {"commands": ["open about:blank"]}, "kind": "browser_segment"},
    )
    assert payload_resp.status_code == 201
    payload_ref = payload_resp.json()["payload_ref"]

    candidate_resp = await client.post(
        "/v1/skills/candidates",
        json={
            "skill_key": skill_key,
            "source_execution_ids": [execution_id],
            "summary": summary,
            "usage_notes": "Requires browser sandbox",
            "preconditions": {"browser": "available"},
            "postconditions": {"result": "returned"},
            "payload_ref": payload_ref,
        },
    )
    assert candidate_resp.status_code == 201
    candidate_id = candidate_resp.json()["id"]

    eval_resp = await client.post(
        f"/v1/skills/candidates/{candidate_id}/evaluate",
        json={"passed": True, "score": 0.9, "benchmark_id": "test-bench"},
    )
    assert eval_resp.status_code == 200

    promote_resp = await client.post(
        f"/v1/skills/candidates/{candidate_id}/promote",
        json={"stage": "canary"},
    )
    assert promote_resp.status_code == 200
    release_id = promote_resp.json()["id"]

    return candidate_id, release_id


# ---------------------------------------------------------------------------
# POST /v1/skills/goals
# ---------------------------------------------------------------------------


async def test_declare_goal_creates_record():
    skill_key = "evol-test-declare-goal"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.post(
            "/v1/skills/goals",
            json={"skill_key": skill_key, "goal": "Return the star count of a GitHub repo."},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["goal_id"].startswith("goal-")
    assert data["skill_key"] == skill_key
    assert data["goal"] == "Return the star count of a GitHub repo."
    assert "rubric_summary" in data


async def test_declare_goal_upsert_updates_goal():
    skill_key = "evol-test-upsert-goal"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        first = await client.post(
            "/v1/skills/goals",
            json={"skill_key": skill_key, "goal": "Original goal."},
        )
        assert first.status_code == 200
        goal_id = first.json()["goal_id"]

        second = await client.post(
            "/v1/skills/goals",
            json={"skill_key": skill_key, "goal": "Updated goal."},
        )
        assert second.status_code == 200

    assert second.json()["goal_id"] == goal_id
    assert second.json()["goal"] == "Updated goal."


# ---------------------------------------------------------------------------
# GET /v1/skills/{skill_key}/active
# ---------------------------------------------------------------------------


async def test_get_active_returns_404_when_no_release():
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.get("/v1/skills/evol-no-such-skill/active")

    assert resp.status_code == 404


async def test_get_active_returns_view_after_promote():
    skill_key = "evol-test-get-active"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        _, release_id = await _create_candidate_and_promote(
            client, skill_key=skill_key, summary="Star count skill"
        )

        resp = await client.get(f"/v1/skills/{skill_key}/active")

    assert resp.status_code == 200
    data = resp.json()
    assert data["skill_key"] == skill_key
    assert data["release_id"] == release_id
    assert data["version"] >= 1
    assert data["stage"] == "canary"
    assert data["goal"] is None
    assert "content" in data
    assert skill_key in data["content"]
    assert "Star count skill" in data["content"]


async def test_get_active_includes_goal_when_declared():
    skill_key = "evol-test-active-with-goal"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        await _create_candidate_and_promote(client, skill_key=skill_key)

        await client.post(
            "/v1/skills/goals",
            json={"skill_key": skill_key, "goal": "Do something specific."},
        )

        resp = await client.get(f"/v1/skills/{skill_key}/active")

    assert resp.status_code == 200
    assert resp.json()["goal"] == "Do something specific."


async def test_get_active_returns_preconditions_and_postconditions():
    skill_key = "evol-test-active-conds"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        await _create_candidate_and_promote(client, skill_key=skill_key)
        resp = await client.get(f"/v1/skills/{skill_key}/active")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["preconditions"], list)
    assert isinstance(data["postconditions"], list)


# ---------------------------------------------------------------------------
# POST /v1/skills/outcomes
# ---------------------------------------------------------------------------


async def test_report_outcome_success_is_accepted():
    skill_key = "evol-test-outcome-success"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        _, release_id = await _create_candidate_and_promote(client, skill_key=skill_key)

        resp = await client.post(
            "/v1/skills/outcomes",
            json={
                "skill_key": skill_key,
                "release_id": release_id,
                "outcome": "success",
                "reasoning": "Star count element found at .social-count. Value: 12345.",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["outcome_id"].startswith("outcome-")
    assert data["accepted"] is True


async def test_report_outcome_failure_with_signals():
    skill_key = "evol-test-outcome-failure"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        _, release_id = await _create_candidate_and_promote(client, skill_key=skill_key)

        resp = await client.post(
            "/v1/skills/outcomes",
            json={
                "skill_key": skill_key,
                "release_id": release_id,
                "outcome": "failure",
                "reasoning": "GitHub layout changed. Star count element not found at .social-count.",  # noqa: E501
                "signals": {"page_load_time_ms": 3200, "element_found": False},
            },
        )

    assert resp.status_code == 201
    assert resp.json()["accepted"] is True


async def test_report_outcome_partial():
    skill_key = "evol-test-outcome-partial"
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        _, release_id = await _create_candidate_and_promote(client, skill_key=skill_key)

        resp = await client.post(
            "/v1/skills/outcomes",
            json={
                "skill_key": skill_key,
                "release_id": release_id,
                "outcome": "partial",
                "reasoning": "Navigation succeeded but data extraction incomplete.",
            },
        )

    assert resp.status_code == 201


async def test_report_outcome_invalid_outcome_rejected():
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.post(
            "/v1/skills/outcomes",
            json={
                "skill_key": "some-skill",
                "release_id": "rel-001",
                "outcome": "bad_value",
                "reasoning": "This should be rejected.",
            },
        )

    assert resp.status_code == 422


async def test_report_outcome_reasoning_is_required():
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        resp = await client.post(
            "/v1/skills/outcomes",
            json={
                "skill_key": "some-skill",
                "release_id": "rel-001",
                "outcome": "success",
            },
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Full round-trip: declare → promote → get_active → report_outcome
# ---------------------------------------------------------------------------


async def test_full_evolution_round_trip():
    """Simulate one complete evolution cycle from an agent's perspective."""
    skill_key = "evol-test-full-round-trip"

    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        # 1. Declare the goal (first thing agent does for a new skill)
        goal_resp = await client.post(
            "/v1/skills/goals",
            json={
                "skill_key": skill_key,
                "goal": "Navigate to a GitHub repository page and return the star count as an integer.",  # noqa: E501
            },
        )
        assert goal_resp.status_code == 200

        # 2. Create + promote a candidate (simulating the extraction pipeline)
        _, release_id = await _create_candidate_and_promote(
            client, skill_key=skill_key, summary="Gets GitHub repo star count"
        )

        # 3. Get the active skill (what the agent reads before executing)
        active_resp = await client.get(f"/v1/skills/{skill_key}/active")
        assert active_resp.status_code == 200
        active = active_resp.json()
        assert active["goal"] == (
            "Navigate to a GitHub repository page and return the star count as an integer."
        )
        assert active["release_id"] == release_id
        assert skill_key in active["content"]

        # 4. Report the outcome after execution (feeds evolution signal)
        outcome_resp = await client.post(
            "/v1/skills/outcomes",
            json={
                "skill_key": skill_key,
                "release_id": release_id,
                "outcome": "success",
                "reasoning": "Navigated to repo page. Star count found at .social-count: 42000.",
                "signals": {"star_count": 42000, "load_time_ms": 800},
            },
        )
        assert outcome_resp.status_code == 201
        assert outcome_resp.json()["accepted"] is True
