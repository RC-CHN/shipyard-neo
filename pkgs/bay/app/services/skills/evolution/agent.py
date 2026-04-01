"""SkillMutationAgent — generates mutated skill candidates via LLM.

Phase 3 additions:
- Injects the declared SkillGoal text into the LLM prompt.
- Generates (or loads cached) SkillRubric via rubric_generator.
- Evaluates the mutated candidate against goal + rubric using an evaluator.
- Auto-promotes the candidate when evaluator score >= auto_promote_threshold.
"""

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.skill import (
    SkillCandidate,
    SkillCandidateStatus,
    SkillGoal,
    SkillOutcome,
    SkillReleaseMode,
    SkillReleaseStage,
)
from app.services.skills.evolution.llm import MutationOutput  # re-exported
from app.services.skills.evolution.meta_prompt import MetaPromptService
from app.services.skills.evolution.rubric import SkillRubric
from app.services.skills.service import SkillLifecycleService, _assemble_skill_content
from app.utils.datetime import utcnow

logger = structlog.get_logger()

__all__ = ["MutationOutput", "SkillMutationAgent"]


class SkillMutationAgent:
    """Creates mutated skill candidates using LLM + MetaPrompt strategy."""

    def __init__(
        self,
        *,
        db_session: AsyncSession,
        llm_client,
        max_recent_outcomes: int = 10,
        evaluator=None,
        auto_promote_threshold: float = 0.7,
        rubric_generator=None,
    ) -> None:
        self._db = db_session
        self._llm = llm_client
        self._max_recent_outcomes = max_recent_outcomes
        self._evaluator = evaluator
        self._auto_promote_threshold = auto_promote_threshold
        self._rubric_generator = rubric_generator
        self._log = logger.bind(component="skill_mutation_agent")

    async def mutate(self, *, owner: str, skill_key: str) -> str | None:
        """Mutate a skill and return the new candidate ID, or None on failure."""
        svc = SkillLifecycleService(self._db)
        meta_svc = MetaPromptService(self._db)

        # Get active release — bail early if none
        release = await svc.get_active_release(owner=owner, skill_key=skill_key)
        if release is None:
            self._log.info("skill_mutation.no_active_release", owner=owner, skill_key=skill_key)
            return None

        # Load parent candidate directly (bypass soft-delete guard — it's active)
        parent_result = await self._db.execute(
            select(SkillCandidate).where(
                SkillCandidate.id == release.candidate_id,
                SkillCandidate.owner == owner,
            )
        )
        parent = parent_result.scalars().first()
        if parent is None:
            return None

        # Collect recent failures as context
        failures_result = await self._db.execute(
            select(SkillOutcome)
            .where(
                SkillOutcome.owner == owner,
                SkillOutcome.skill_key == skill_key,
                SkillOutcome.outcome == "failure",
            )
            .order_by(SkillOutcome.created_at.desc())
            .limit(self._max_recent_outcomes)
        )
        recent_failures = list(failures_result.scalars().all())

        # Ensure meta-prompts exist, then sample one
        meta_prompt = await meta_svc.sample_prompt()
        if meta_prompt is None:
            await meta_svc.seed_defaults()
            meta_prompt = await meta_svc.sample_prompt()
        if meta_prompt is None:
            self._log.warning("skill_mutation.no_meta_prompt", owner=owner, skill_key=skill_key)
            return None

        # Phase 3: load declared goal and cached rubric (optional)
        goal_result = await self._db.execute(
            select(SkillGoal).where(
                SkillGoal.owner == owner,
                SkillGoal.skill_key == skill_key,
            )
        )
        skill_goal = goal_result.scalars().first()
        goal_text: str | None = skill_goal.goal if skill_goal is not None else None

        # Load or generate rubric when goal + generator are available
        rubric: SkillRubric | None = None
        if skill_goal is not None and self._rubric_generator is not None:
            rubric = await self._load_or_generate_rubric(skill_goal)

        # Decode parent pre/postconditions for the LLM prompt
        preconditions = _decode_conditions(parent.preconditions_json)
        postconditions = _decode_conditions(parent.postconditions_json)

        # Build LLM prompt from parent content (the "current" state being mutated)
        parent_content = _assemble_skill_content(
            skill_key=skill_key,
            summary=parent.summary,
            usage_notes=parent.usage_notes,
            preconditions=preconditions,
            postconditions=postconditions,
        )
        failure_lines = "\n".join(
            f"- Failure {i + 1}: {o.reasoning}" for i, o in enumerate(recent_failures)
        )
        goal_section = f"## Goal\n\n{goal_text}\n\n" if goal_text else ""
        prompt = (
            f"{goal_section}"
            f"## Current Skill Content\n\n{parent_content}\n\n"
            f"## Recent Failures\n\n{failure_lines or '(none recorded)'}\n\n"
            f"## Mutation Strategy\n\n{meta_prompt.instruction}"
        )

        # Call LLM
        try:
            mutation: MutationOutput = await self._llm.generate_mutation(prompt)
        except Exception as exc:
            self._log.warning(
                "skill_mutation.llm_failed",
                owner=owner,
                skill_key=skill_key,
                error=str(exc),
            )
            return None

        # Persist mutated candidate (inherits payload from parent, no new source executions)
        now = utcnow()
        new_candidate = SkillCandidate(
            id=f"sc-{uuid.uuid4().hex[:12]}",
            owner=owner,
            skill_key=skill_key,
            source_execution_ids=parent.source_execution_ids or "",
            payload_ref=parent.payload_ref,
            payload_hash=parent.payload_hash,
            skill_type=parent.skill_type,
            summary=mutation.summary,
            usage_notes=mutation.usage_notes,
            preconditions_json=json.dumps(mutation.preconditions, ensure_ascii=False),
            postconditions_json=json.dumps(mutation.postconditions, ensure_ascii=False),
            status=SkillCandidateStatus.DRAFT,
            created_by="system:evolution",
            evolution_parent_id=parent.id,
            evolution_meta_prompt_id=meta_prompt.id,
            mutation_reasoning=mutation.mutation_reasoning,
            created_at=now,
            updated_at=now,
        )
        self._db.add(new_candidate)
        await self._db.commit()
        await self._db.refresh(new_candidate)

        # Track meta-prompt usage for weighted sampling
        await meta_svc.record_usage(meta_prompt.id)

        self._log.info(
            "skill_mutation.created_candidate",
            owner=owner,
            skill_key=skill_key,
            candidate_id=new_candidate.id,
            meta_prompt_id=meta_prompt.id,
        )

        # Phase 3: evaluate against mutated content + rubric, then optionally promote
        if self._evaluator is not None and goal_text is not None:
            # Assemble content from the *mutation output* — not the parent
            mutated_content = _assemble_skill_content(
                skill_key=skill_key,
                summary=mutation.summary,
                usage_notes=mutation.usage_notes,
                preconditions=mutation.preconditions,
                postconditions=mutation.postconditions,
            )
            await self._auto_evaluate_and_promote(
                svc=svc,
                candidate=new_candidate,
                goal=goal_text,
                rubric=rubric,
                skill_content=mutated_content,
                failure_context=failure_lines or "(none recorded)",
                owner=owner,
            )

        return new_candidate.id

    async def _load_or_generate_rubric(self, skill_goal: SkillGoal) -> SkillRubric | None:
        """Return the cached rubric from SkillGoal, or generate+persist a new one."""
        if skill_goal.rubric_json:
            try:
                rubric = SkillRubric.model_validate_json(skill_goal.rubric_json)
                # Backfill legacy rows that cached rubric_json before rubric_summary
                # started being persisted alongside it.
                if not skill_goal.rubric_summary:
                    skill_goal.rubric_summary = rubric.summary
                    skill_goal.updated_at = utcnow()
                    await self._db.commit()
                return rubric
            except Exception:
                pass  # regenerate if cached JSON is malformed

        rubric = await self._rubric_generator.generate(skill_goal.goal)
        if rubric is not None:
            skill_goal.rubric_json = rubric.model_dump_json()
            skill_goal.rubric_summary = rubric.summary
            skill_goal.updated_at = utcnow()
            await self._db.commit()
            self._log.info(
                "skill_mutation.rubric_generated",
                skill_key=skill_goal.skill_key,
            )
        return rubric

    async def _auto_evaluate_and_promote(
        self,
        *,
        svc: SkillLifecycleService,
        candidate: SkillCandidate,
        goal: str,
        rubric: SkillRubric | None,
        skill_content: str,
        failure_context: str,
        owner: str,
    ) -> None:
        """Evaluate the mutation and auto-promote if it passes the threshold.

        Only records a formal evaluation and promotes when the evaluator returns
        a passing score at or above the threshold.  Below-threshold candidates
        remain DRAFT for human review — we don't mark them REJECTED so they can
        still be promoted manually.
        """
        try:
            result = await self._evaluator.evaluate(
                goal=goal,
                rubric=rubric,
                skill_content=skill_content,
                failure_context=failure_context,
            )
        except Exception as exc:
            self._log.warning(
                "skill_mutation.evaluator_failed",
                candidate_id=candidate.id,
                error=str(exc),
            )
            return

        if result is None:
            return

        should_promote = result.passed and result.score >= self._auto_promote_threshold

        if should_promote:
            # Record evaluation then promote in one sweep
            await svc.evaluate_candidate(
                owner=owner,
                candidate_id=candidate.id,
                passed=True,
                score=result.score,
                report=result.reasoning,
                evaluated_by="system:evolution",
            )
            try:
                await svc.promote_candidate(
                    owner=owner,
                    candidate_id=candidate.id,
                    stage=SkillReleaseStage.CANARY,
                    release_mode=SkillReleaseMode.AUTO,
                )
                self._log.info(
                    "skill_mutation.auto_promoted",
                    candidate_id=candidate.id,
                    score=result.score,
                )
            except Exception as exc:
                self._log.warning(
                    "skill_mutation.auto_promote_failed",
                    candidate_id=candidate.id,
                    error=str(exc),
                )
        else:
            self._log.info(
                "skill_mutation.evaluation_below_threshold",
                candidate_id=candidate.id,
                passed=result.passed,
                score=result.score,
                threshold=self._auto_promote_threshold,
            )


def _decode_conditions(json_str: str | None) -> list[str]:
    """Parse a preconditions/postconditions JSON string into a list of strings."""
    if not json_str:
        return []
    try:
        raw = json.loads(json_str)
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if isinstance(raw, dict):
            return [f"{k}: {v}" for k, v in raw.items()]
    except Exception:
        pass
    return []
