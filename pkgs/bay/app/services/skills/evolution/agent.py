"""SkillMutationAgent — generates mutated skill candidates via LLM."""

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.skill import SkillCandidate, SkillCandidateStatus, SkillOutcome
from app.services.skills.evolution.llm import MutationOutput  # re-exported
from app.services.skills.evolution.meta_prompt import MetaPromptService
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
    ) -> None:
        self._db = db_session
        self._llm = llm_client
        self._max_recent_outcomes = max_recent_outcomes
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

        # Decode parent pre/postconditions
        preconditions = _decode_conditions(parent.preconditions_json)
        postconditions = _decode_conditions(parent.postconditions_json)

        # Build LLM prompt
        current_content = _assemble_skill_content(
            skill_key=skill_key,
            summary=parent.summary,
            usage_notes=parent.usage_notes,
            preconditions=preconditions,
            postconditions=postconditions,
        )
        failure_lines = "\n".join(
            f"- Failure {i + 1}: {o.reasoning}" for i, o in enumerate(recent_failures)
        )
        prompt = (
            f"## Current Skill Content\n\n{current_content}\n\n"
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
        return new_candidate.id


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
