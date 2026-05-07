"""EvolutionScheduler — periodic scan and mutation triggering."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import EvolutionConfig
from app.models.skill import SkillCandidate, SkillGoal, SkillOutcome

logger = structlog.get_logger()


@dataclass
class EvolutionCycleResult:
    """Result of a single evolution cycle."""

    mutations_attempted: int = field(default=0)
    mutations_succeeded: int = field(default=0)


class EvolutionScheduler:
    """Scans for skills with sufficient failures and triggers mutations up to a budget cap."""

    def __init__(
        self,
        *,
        db_session: AsyncSession,
        config: EvolutionConfig,
        mutation_agent,
    ) -> None:
        self._db = db_session
        self._config = config
        self._agent = mutation_agent
        self._log = logger.bind(component="evolution_scheduler")

    async def run_cycle(self) -> EvolutionCycleResult:
        """Run one evolution cycle. Returns a summary of what happened."""
        result = EvolutionCycleResult()

        if not self._config.enabled:
            self._log.debug("evolution_scheduler.disabled")
            return result

        # All declared skill goals define the evolution scope
        goals_result = await self._db.execute(select(SkillGoal))
        goals = list(goals_result.scalars().all())

        budget = self._config.max_mutations_per_cycle

        for goal in goals:
            if result.mutations_attempted >= budget:
                self._log.debug("evolution_scheduler.budget_exhausted", budget=budget)
                break

            # Count recent failures for this skill within the lookback window
            failures_result = await self._db.execute(
                select(SkillOutcome)
                .where(
                    SkillOutcome.owner == goal.owner,
                    SkillOutcome.skill_key == goal.skill_key,
                    SkillOutcome.outcome == "failure",
                )
                .order_by(SkillOutcome.created_at.desc())
                .limit(self._config.max_recent_outcomes)
            )
            recent_failures = list(failures_result.scalars().all())

            if len(recent_failures) < self._config.min_failures_to_trigger:
                self._log.debug(
                    "evolution_scheduler.skip_insufficient_failures",
                    owner=goal.owner,
                    skill_key=goal.skill_key,
                    failure_count=len(recent_failures),
                    min_required=self._config.min_failures_to_trigger,
                )
                continue

            # Dedup: skip if an evolution candidate was already created after the latest failure.
            # This prevents the scheduler from firing again every cycle on the same failure set.
            latest_failure = recent_failures[0]  # already sorted desc
            latest_mutation_result = await self._db.execute(
                select(SkillCandidate)
                .where(
                    SkillCandidate.owner == goal.owner,
                    SkillCandidate.skill_key == goal.skill_key,
                    SkillCandidate.created_by == "system:evolution",
                    SkillCandidate.is_deleted.is_(False),
                )
                .order_by(SkillCandidate.created_at.desc())
                .limit(1)
            )
            latest_mutation = latest_mutation_result.scalars().first()
            already_mutated = (
                latest_mutation is not None
                and latest_mutation.created_at >= latest_failure.created_at
            )
            if already_mutated:
                self._log.debug(
                    "evolution_scheduler.skip_already_mutated",
                    owner=goal.owner,
                    skill_key=goal.skill_key,
                    last_mutation_at=str(latest_mutation.created_at),
                    last_failure_at=str(latest_failure.created_at),
                )
                continue

            self._log.info(
                "evolution_scheduler.triggering_mutation",
                owner=goal.owner,
                skill_key=goal.skill_key,
                failure_count=len(recent_failures),
            )

            result.mutations_attempted += 1
            new_candidate_id = await self._agent.mutate(
                owner=goal.owner,
                skill_key=goal.skill_key,
            )
            if new_candidate_id is not None:
                result.mutations_succeeded += 1

        self._log.info(
            "evolution_scheduler.cycle_complete",
            attempted=result.mutations_attempted,
            succeeded=result.mutations_succeeded,
        )
        return result
