"""Evolution scheduler lifecycle — init, run, shutdown."""

from __future__ import annotations

import asyncio

import structlog

from app.config import EvolutionConfig, get_settings
from app.db.session import get_async_session
from app.services.skills.evolution.agent import SkillMutationAgent
from app.services.skills.evolution.evaluator import GoalConditionedEvaluator
from app.services.skills.evolution.llm import (
    LlmEvolutionClient,
    make_evaluator_client,
    make_rubric_client,
)
from app.services.skills.evolution.meta_prompt import MetaPromptService
from app.services.skills.evolution.rubric import RubricGenerator
from app.services.skills.evolution.scheduler import EvolutionCycleResult, EvolutionScheduler

logger = structlog.get_logger()


class EvolutionSchedulerRunner:
    """Wraps EvolutionScheduler in a periodic background task."""

    def __init__(self, config: EvolutionConfig) -> None:
        self._config = config
        self._running = False
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._log = logger.bind(service="evolution_scheduler_runner")

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_once(self) -> EvolutionCycleResult:
        async with self._run_lock:
            return await self._run_cycle()

    async def _run_cycle(self) -> EvolutionCycleResult:
        settings = get_settings()
        config = settings.evolution

        if not config.enabled:
            return EvolutionCycleResult()

        if not config.llm.enabled:
            self._log.debug("skills.evolution.llm.disabled")
            return EvolutionCycleResult()

        async with get_async_session() as db_session:
            # Seed meta-prompts on first run
            meta_svc = MetaPromptService(db_session)
            await meta_svc.seed_defaults()

            llm_client = LlmEvolutionClient(config.llm)
            rubric_generator = RubricGenerator(llm_client=make_rubric_client(config.llm))
            evaluator = GoalConditionedEvaluator(llm_client=make_evaluator_client(config.llm))
            agent = SkillMutationAgent(
                db_session=db_session,
                llm_client=llm_client,
                max_recent_outcomes=config.max_recent_outcomes,
                evaluator=evaluator,
                auto_promote_threshold=config.auto_promote_threshold,
                rubric_generator=rubric_generator,
            )
            scheduler = EvolutionScheduler(
                db_session=db_session,
                config=config,
                mutation_agent=agent,
            )
            result = await scheduler.run_cycle()
            self._log.info(
                "skills.evolution.cycle_complete",
                attempted=result.mutations_attempted,
                succeeded=result.mutations_succeeded,
            )
            return result

    async def start(self) -> None:
        if self._running:
            self._log.warning("skills.evolution.scheduler.already_running")
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        self._log.info(
            "skills.evolution.scheduler.started",
            interval_seconds=self._config.interval_seconds,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._log.info("skills.evolution.scheduler.stopped")

    async def _background_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._config.interval_seconds)
                if self._running:
                    await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.exception(
                    "skills.evolution.scheduler.cycle_error",
                    error=str(exc),
                )


# ---------------------------------------------------------------------------
# Module-level singleton (matches pattern of browser_learning lifecycle)
# ---------------------------------------------------------------------------

_evolution_runner: EvolutionSchedulerRunner | None = None


async def init_evolution_scheduler() -> EvolutionSchedulerRunner:
    """Initialize evolution scheduler and optionally start it."""
    global _evolution_runner

    settings = get_settings()
    config = settings.evolution
    _evolution_runner = EvolutionSchedulerRunner(config=config)

    logger.info(
        "skills.evolution.scheduler.init",
        enabled=config.enabled,
        run_on_startup=config.run_on_startup,
        interval_seconds=config.interval_seconds,
        min_failures=config.min_failures_to_trigger,
        max_mutations=config.max_mutations_per_cycle,
    )

    if not config.enabled:
        return _evolution_runner

    if config.run_on_startup:
        try:
            await _evolution_runner.run_once()
        except Exception as exc:
            logger.exception(
                "skills.evolution.scheduler.startup_cycle_failed",
                error=str(exc),
            )

    await _evolution_runner.start()
    return _evolution_runner


async def shutdown_evolution_scheduler() -> None:
    """Stop evolution scheduler."""
    global _evolution_runner
    if _evolution_runner is not None:
        await _evolution_runner.stop()
        _evolution_runner = None


def get_evolution_scheduler() -> EvolutionSchedulerRunner | None:
    """Get global evolution scheduler instance."""
    return _evolution_runner
