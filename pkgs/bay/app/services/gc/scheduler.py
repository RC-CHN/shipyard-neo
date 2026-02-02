"""GC Scheduler - orchestrates GC task execution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from app.services.gc.base import GCResult, GCTask
from app.services.gc.coordinator import GCCoordinator, NoopCoordinator

if TYPE_CHECKING:
    from app.config import GCConfig

logger = structlog.get_logger()


class GCScheduler:
    """Scheduler for GC tasks.

    Responsibilities:
    - Manage background loop for periodic GC execution
    - Execute tasks serially in defined order
    - Handle task errors without stopping the scheduler
    - Coordinate with other instances (via coordinator)

    Usage:
        scheduler = GCScheduler(
            tasks=[IdleSessionGC(...), ExpiredSandboxGC(...)],
            config=settings.gc,
        )

        # Run once immediately
        await scheduler.run_once()

        # Start background loop
        await scheduler.start()

        # Stop gracefully
        await scheduler.stop()
    """

    def __init__(
        self,
        tasks: list[GCTask],
        config: "GCConfig",
        coordinator: GCCoordinator | None = None,
    ) -> None:
        """Initialize GC scheduler.

        Args:
            tasks: List of GC tasks to execute (in order)
            config: GC configuration
            coordinator: Coordination strategy (default: NoopCoordinator)
        """
        self._tasks = tasks
        self._config = config
        self._coordinator = coordinator or NoopCoordinator()
        self._log = logger.bind(service="gc_scheduler")

        # Background loop state
        self._running = False
        self._task: asyncio.Task | None = None

        # Mutex to prevent concurrent run_once / background loop overlap
        self._run_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Whether the background loop is running."""
        return self._running

    async def run_once(self) -> list[GCResult]:
        """Execute one GC cycle.

        This method is thread-safe and reentrant-safe.
        If another cycle is in progress, this call will wait.

        Returns:
            List of GCResult for each task
        """
        async with self._run_lock:
            return await self._run_cycle()

    async def _run_cycle(self) -> list[GCResult]:
        """Internal: Execute one GC cycle (not thread-safe, use run_once)."""
        self._log.info("gc.cycle.start")

        results: list[GCResult] = []

        async with self._coordinator.acquire() as acquired:
            if not acquired:
                self._log.info("gc.cycle.skipped", reason="coordination_lock_not_acquired")
                return results

            for task in self._tasks:
                result = await self._run_task(task)
                results.append(result)

        self._log.info(
            "gc.cycle.complete",
            total_cleaned=sum(r.cleaned_count for r in results),
            total_errors=sum(len(r.errors) for r in results),
        )

        return results

    async def _run_task(self, task: GCTask) -> GCResult:
        """Execute a single GC task with error handling."""
        self._log.info("gc.task.start", task=task.name)

        try:
            result = await task.run()
            result.task_name = task.name

            self._log.info(
                "gc.task.complete",
                task=task.name,
                cleaned=result.cleaned_count,
                skipped=result.skipped_count,
                errors=len(result.errors),
            )

            if result.errors:
                for error in result.errors:
                    self._log.warning(
                        "gc.task.item_error",
                        task=task.name,
                        error=error,
                    )

            return result

        except Exception as e:
            self._log.exception(
                "gc.task.failed",
                task=task.name,
                error=str(e),
            )
            result = GCResult(task_name=task.name)
            result.add_error(f"Task failed: {e}")
            return result

    async def start(self) -> None:
        """Start background GC loop.

        The loop runs periodically based on config.interval_seconds.
        Call stop() to gracefully shut down.
        """
        if self._running:
            self._log.warning("gc.scheduler.already_running")
            return

        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        self._log.info(
            "gc.scheduler.started",
            interval_seconds=self._config.interval_seconds,
        )

    async def stop(self) -> None:
        """Stop background GC loop gracefully.

        Waits for current cycle to complete before returning.
        """
        if not self._running:
            return

        self._log.info("gc.scheduler.stopping")
        self._running = False

        if self._task is not None:
            # Cancel the sleep, not the task execution
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        self._log.info("gc.scheduler.stopped")

    async def _background_loop(self) -> None:
        """Internal background loop."""
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                self._log.exception("gc.scheduler.cycle_error", error=str(e))

            # Sleep until next cycle (or until cancelled)
            try:
                await asyncio.sleep(self._config.interval_seconds)
            except asyncio.CancelledError:
                break
