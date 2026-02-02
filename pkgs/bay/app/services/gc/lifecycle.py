"""GC lifecycle management for FastAPI lifespan integration."""

from __future__ import annotations

import structlog

from app.api.dependencies import get_driver
from app.config import get_settings
from app.db.session import get_async_session
from app.services.gc.base import GCTask
from app.services.gc.scheduler import GCScheduler
from app.services.gc.tasks import (
    ExpiredSandboxGC,
    IdleSessionGC,
    OrphanContainerGC,
    OrphanCargoGC,
)

logger = structlog.get_logger()

# Global scheduler instance
_gc_scheduler: GCScheduler | None = None


def _create_gc_tasks_factory():
    """Create a factory function that produces GC tasks with fresh db sessions.

    Each GC cycle will create new db sessions for its tasks, ensuring
    proper transaction isolation and avoiding stale session issues.

    Returns:
        List of GCTask instances
    """
    settings = get_settings()
    driver = get_driver()
    gc_config = settings.gc

    async def create_tasks():
        """Create tasks with fresh db sessions."""
        async with get_async_session() as db_session:
            tasks: list[GCTask] = []

            if gc_config.idle_session.enabled:
                tasks.append(IdleSessionGC(driver, db_session))

            if gc_config.expired_sandbox.enabled:
                tasks.append(ExpiredSandboxGC(driver, db_session))

            if gc_config.orphan_cargo.enabled:
                tasks.append(OrphanCargoGC(driver, db_session))

            if gc_config.orphan_container.enabled:
                tasks.append(OrphanContainerGC(driver, db_session, gc_config))

            return tasks

    return create_tasks


class SessionPerCycleGCScheduler(GCScheduler):
    """GC Scheduler that creates fresh db sessions for each cycle.

    This ensures proper transaction handling and avoids stale session issues
    that could occur in long-running background tasks.
    """

    def __init__(self, config):
        # Initialize with empty tasks - they'll be created per-cycle
        super().__init__(tasks=[], config=config)
        self._config = config
        self._driver = get_driver()

    async def _run_cycle(self):
        """Execute one GC cycle with fresh db sessions."""
        settings = get_settings()
        gc_config = settings.gc

        self._log.info("gc.cycle.start")

        results = []

        async with self._coordinator.acquire() as acquired:
            if not acquired:
                self._log.info("gc.cycle.skipped", reason="coordination_lock_not_acquired")
                return results

            # Create fresh db session for this cycle
            async with get_async_session() as db_session:
                # Build tasks with fresh session
                tasks: list[GCTask] = []

                if gc_config.idle_session.enabled:
                    tasks.append(IdleSessionGC(self._driver, db_session))

                if gc_config.expired_sandbox.enabled:
                    tasks.append(ExpiredSandboxGC(self._driver, db_session))

                if gc_config.orphan_cargo.enabled:
                    tasks.append(OrphanCargoGC(self._driver, db_session))

                if gc_config.orphan_container.enabled:
                    tasks.append(OrphanContainerGC(self._driver, db_session, gc_config))

                # Execute tasks
                for task in tasks:
                    result = await self._run_task(task)
                    results.append(result)

        self._log.info(
            "gc.cycle.complete",
            total_cleaned=sum(r.cleaned_count for r in results),
            total_errors=sum(len(r.errors) for r in results),
        )

        return results


async def init_gc_scheduler() -> GCScheduler | None:
    """Initialize the GC scheduler.

    Called during FastAPI lifespan startup, after database initialization.

    The scheduler is ALWAYS created (for Admin API manual trigger support),
    but the background loop is only started if gc.enabled=true.

    Returns:
        GCScheduler instance (always created for Admin API support)
    """
    global _gc_scheduler

    settings = get_settings()
    gc_config = settings.gc

    logger.info(
        "gc.init",
        enabled=gc_config.enabled,
        instance_id=gc_config.get_instance_id(),
        interval_seconds=gc_config.interval_seconds,
        run_on_startup=gc_config.run_on_startup,
        tasks={
            "idle_session": gc_config.idle_session.enabled,
            "expired_sandbox": gc_config.expired_sandbox.enabled,
            "orphan_cargo": gc_config.orphan_cargo.enabled,
            "orphan_container": gc_config.orphan_container.enabled,
        },
    )

    # Always create scheduler (for Admin API manual trigger support)
    _gc_scheduler = SessionPerCycleGCScheduler(config=gc_config)

    # If GC is disabled, don't run on startup or start background loop
    if not gc_config.enabled:
        logger.info("gc.background_disabled", reason="gc.enabled=false")
        return _gc_scheduler

    # Run once on startup if configured
    if gc_config.run_on_startup:
        logger.info("gc.run_on_startup.start")
        try:
            results = await _gc_scheduler.run_once()
            total_cleaned = sum(r.cleaned_count for r in results)
            total_errors = sum(len(r.errors) for r in results)
            logger.info(
                "gc.run_on_startup.complete",
                cleaned=total_cleaned,
                errors=total_errors,
            )
        except Exception as e:
            logger.exception("gc.run_on_startup.failed", error=str(e))
            # Don't fail startup due to GC errors

    # Start background loop
    await _gc_scheduler.start()

    return _gc_scheduler


async def shutdown_gc_scheduler() -> None:
    """Stop the GC scheduler gracefully.

    Called during FastAPI lifespan shutdown.
    """
    global _gc_scheduler

    if _gc_scheduler is not None:
        await _gc_scheduler.stop()
        _gc_scheduler = None


def get_gc_scheduler() -> GCScheduler | None:
    """Get the current GC scheduler instance (for testing/monitoring)."""
    return _gc_scheduler
