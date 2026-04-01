"""Unit tests for skill evolution API endpoints (Phase 1).

Tests cover:
- POST /v1/skills/goals (declare_skill_goal)
- GET  /v1/skills/{skill_key}/active (get_active_skill)
- POST /v1/skills/outcomes (report_skill_outcome)

Uses fake service objects — no DB or HTTP involved.
"""

from __future__ import annotations

import pytest

from app.api.v1.skills import (
    SkillActiveResponse,
    SkillCandidateCreateRequest,
    SkillGoalDeclareRequest,
    SkillOutcomeRequest,
    declare_skill_goal,
    get_active_skill,
    report_skill_outcome,
)
from app.errors import NotFoundError

# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------


class _FakeGoal:
    def __init__(self, *, skill_key: str, goal: str, updated: bool = False):
        self.id = "goal-abc123"
        self.skill_key = skill_key
        self.goal = goal
        self.rubric_summary = ""
        self.updated = updated


class _FakeOutcome:
    def __init__(self):
        self.id = "outcome-xyz789"


class _FakeSkillService:
    def __init__(self, *, active_view: dict | None = None):
        self.declare_goal_calls: list[dict] = []
        self.record_outcome_calls: list[dict] = []
        self._active_view = active_view
        self._declare_goal_returns_upserted = False

    async def declare_goal(self, *, owner: str, skill_key: str, goal: str):
        self.declare_goal_calls.append(
            {"owner": owner, "skill_key": skill_key, "goal": goal}
        )
        return _FakeGoal(skill_key=skill_key, goal=goal)

    async def get_active_skill_view(self, *, owner: str, skill_key: str):
        if self._active_view is None:
            return None
        return self._active_view

    async def record_outcome(
        self,
        *,
        owner: str,
        skill_key: str,
        release_id: str,
        outcome: str,
        reasoning: str,
        execution_id: str | None = None,
        signals: dict | None = None,
    ):
        self.record_outcome_calls.append(
            {
                "owner": owner,
                "skill_key": skill_key,
                "release_id": release_id,
                "outcome": outcome,
                "reasoning": reasoning,
                "execution_id": execution_id,
                "signals": signals,
            }
        )
        return _FakeOutcome()


# ---------------------------------------------------------------------------
# POST /v1/skills/goals
# ---------------------------------------------------------------------------


class TestDeclareSkillGoal:
    async def test_returns_goal_id_and_fields(self):
        svc = _FakeSkillService()

        response = await declare_skill_goal(
            request=SkillGoalDeclareRequest(
                skill_key="github-get-stars",
                goal="Return the star count of a GitHub repo.",
            ),
            skill_svc=svc,
            owner="default",
        )

        assert response.goal_id == "goal-abc123"
        assert response.skill_key == "github-get-stars"
        assert response.goal == "Return the star count of a GitHub repo."
        assert response.rubric_summary == ""

    async def test_passes_owner_to_service(self):
        svc = _FakeSkillService()

        await declare_skill_goal(
            request=SkillGoalDeclareRequest(skill_key="sk", goal="goal"),
            skill_svc=svc,
            owner="alice",
        )

        assert svc.declare_goal_calls[0]["owner"] == "alice"

    async def test_passes_skill_key_and_goal_to_service(self):
        svc = _FakeSkillService()

        await declare_skill_goal(
            request=SkillGoalDeclareRequest(skill_key="my-skill", goal="My goal."),
            skill_svc=svc,
            owner="default",
        )

        call = svc.declare_goal_calls[0]
        assert call["skill_key"] == "my-skill"
        assert call["goal"] == "My goal."


def test_skill_candidate_create_request_accepts_list_conditions():
    request = SkillCandidateCreateRequest(
        skill_key="browser-login",
        source_execution_ids=["exec-1"],
        preconditions=["browser available", "user authenticated"],
        postconditions=["dashboard visible"],
    )

    assert request.preconditions == ["browser available", "user authenticated"]
    assert request.postconditions == ["dashboard visible"]


# ---------------------------------------------------------------------------
# GET /v1/skills/{skill_key}/active
# ---------------------------------------------------------------------------


class TestGetActiveSkill:
    async def test_raises_not_found_when_no_active_release(self):
        svc = _FakeSkillService(active_view=None)

        with pytest.raises(NotFoundError):
            await get_active_skill(skill_key="missing-skill", skill_svc=svc, owner="default")

    async def test_returns_skill_view_fields(self):
        view = {
            "skill_key": "github-get-stars",
            "release_id": "rel-001",
            "version": 3,
            "stage": "stable",
            "goal": "Return star count.",
            "content": "# SKILL: github-get-stars\n\n## Summary\nReturn star count.",
            "summary": "Return star count.",
            "preconditions": ["browser available"],
            "postconditions": ["integer returned"],
            "payload_ref": "blob:abc123",
        }
        svc = _FakeSkillService(active_view=view)

        response = await get_active_skill(
            skill_key="github-get-stars", skill_svc=svc, owner="default"
        )

        assert isinstance(response, SkillActiveResponse)
        assert response.skill_key == "github-get-stars"
        assert response.release_id == "rel-001"
        assert response.version == 3
        assert response.stage == "stable"
        assert response.goal == "Return star count."
        assert "# SKILL:" in response.content
        assert response.payload_ref == "blob:abc123"

    async def test_returns_skill_view_without_goal(self):
        view = {
            "skill_key": "no-goal-skill",
            "release_id": "rel-002",
            "version": 1,
            "stage": "canary",
            "goal": None,
            "content": "# SKILL: no-goal-skill",
            "summary": None,
            "preconditions": [],
            "postconditions": [],
            "payload_ref": None,
        }
        svc = _FakeSkillService(active_view=view)

        response = await get_active_skill(
            skill_key="no-goal-skill", skill_svc=svc, owner="default"
        )

        assert response.goal is None
        assert response.payload_ref is None


# ---------------------------------------------------------------------------
# POST /v1/skills/outcomes
# ---------------------------------------------------------------------------


class TestReportSkillOutcome:
    async def test_success_outcome_is_accepted(self):
        svc = _FakeSkillService()

        response = await report_skill_outcome(
            request=SkillOutcomeRequest(
                skill_key="github-get-stars",
                release_id="rel-001",
                outcome="success",
                reasoning="Star count found at expected location.",
            ),
            skill_svc=svc,
            owner="default",
        )

        assert response.outcome_id == "outcome-xyz789"
        assert response.accepted is True

    async def test_failure_outcome_with_signals(self):
        svc = _FakeSkillService()

        await report_skill_outcome(
            request=SkillOutcomeRequest(
                skill_key="github-get-stars",
                release_id="rel-001",
                outcome="failure",
                reasoning="Element not found.",
                execution_id="exec-123",
                signals={"element_found": False},
            ),
            skill_svc=svc,
            owner="default",
        )

        call = svc.record_outcome_calls[0]
        assert call["outcome"] == "failure"
        assert call["execution_id"] == "exec-123"
        assert call["signals"] == {"element_found": False}

    async def test_partial_outcome(self):
        svc = _FakeSkillService()

        response = await report_skill_outcome(
            request=SkillOutcomeRequest(
                skill_key="ui-skill",
                release_id="rel-002",
                outcome="partial",
                reasoning="Partial completion.",
            ),
            skill_svc=svc,
            owner="default",
        )

        assert response.accepted is True
        assert svc.record_outcome_calls[0]["outcome"] == "partial"

    async def test_passes_owner_to_service(self):
        svc = _FakeSkillService()

        await report_skill_outcome(
            request=SkillOutcomeRequest(
                skill_key="sk",
                release_id="rel-1",
                outcome="success",
                reasoning="OK.",
            ),
            skill_svc=svc,
            owner="alice",
        )

        assert svc.record_outcome_calls[0]["owner"] == "alice"

    async def test_invalid_outcome_rejected_by_schema(self):
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            SkillOutcomeRequest(
                skill_key="sk",
                release_id="rel-1",
                outcome="invalid_value",
                reasoning="This should fail.",
            )
