"""WarmupQueue - In-process bounded queue for warmup throttling.

Provides a fixed-worker, bounded-queue, dedup-by-sandbox_id layer
between the create endpoint and the heavy ensure_running() call.

Key design decisions (from §2.5 / §2.5.1):
- Create endpoint only enqueues, never executes warmup directly
- Workers consume from queue, call ensure_running() with idempotent checks
- Bounded queue + dedup avoids unbounded resource consumption
- Drop policy (configurable) only affects warmup hit rate, not correctness
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from app.config import WarmPoolConfig

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class WarmupTask:
    """A warmup task to be consumed by workers."""

    sandbox_id: str
    owner: str


@dataclass
class WarmupQueueStats:
    """Observable statistics for the warmup queue."""

    enqueue_total: int = 0
    dedup_total: int = 0
    drop_total: int = 0
    consumed_total: int = 0
    success_total: int = 0
    failure_total: int = 0
    active_workers: int = 0


class WarmupQueue:
    """In-process bounded queue with fixed workers for warmup throttling.

    Thread-safe (asyncio): all operations are coroutine-safe.

    Usage:
        queue = WarmupQueue(config=settings.warm_pool)
        await queue.start()

        # Enqueue warmup (fire-and-forget, may drop if full)
        queue.enqueue(sandbox_id="sandbox-123", owner="default")

        # Graceful shutdown
        await queue.stop()
    """

    def __init__(
        self,
        config: "WarmPoolConfig",
    ) -> None:
        self._config = config
        self._log = logger.bind(service="warmup_queue")

        self._queue: asyncio.Queue[WarmupTask] = asyncio.Queue(
            maxsize=config.warmup_queue_max_size,
        )
        # Dedup set: sandbox_ids currently in the queue
        self._pending: set[str] = set()
        self._pending_lock = asyncio.Lock()

        self._workers: list[asyncio.Task] = []
        self._running = False
        self._stats = WarmupQueueStats()

    @property
    def stats(self) -> WarmupQueueStats:
        """Get current queue statistics (read-only snapshot)."""
        return self._stats

    @property
    def depth(self) -> int:
        """Current queue depth."""
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        """Whether the queue workers are running."""
        return self._running

    async def start(self) -> None:
        """Start worker tasks."""
        if self._running:
            self._log.warning("warmup_queue.already_running")
            return

        self._running = True
        num_workers = self._config.warmup_queue_workers

        for i in range(num_workers):
            task = asyncio.create_task(
                self._worker_loop(worker_id=i),
                name=f"warmup-worker-{i}",
            )
            self._workers.append(task)

        self._log.info(
            "warmup_queue.started",
            workers=num_workers,
            max_size=self._config.warmup_queue_max_size,
            drop_policy=self._config.warmup_queue_drop_policy,
        )

    async def stop(self) -> None:
        """Stop worker tasks gracefully.

        Drains remaining items then cancels workers.
        """
        if not self._running:
            return

        self._log.info("warmup_queue.stopping")
        self._running = False

        # Signal workers to exit by putting sentinel None values
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                pass

        # Wait for workers with timeout
        if self._workers:
            done, pending = await asyncio.wait(self._workers, timeout=10)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._workers.clear()
        self._log.info(
            "warmup_queue.stopped",
            stats_enqueue=self._stats.enqueue_total,
            stats_drop=self._stats.drop_total,
            stats_consumed=self._stats.consumed_total,
        )

    def enqueue(self, *, sandbox_id: str, owner: str) -> bool:
        """Enqueue a warmup task (non-blocking).

        Returns True if enqueued, False if dropped (dedup or full).
        This method is synchronous to avoid blocking the create endpoint.
        """
        # Fast-path dedup check (best-effort, not locked)
        if sandbox_id in self._pending:
            self._stats.dedup_total += 1
            self._log.debug(
                "warmup_queue.dedup",
                sandbox_id=sandbox_id,
            )
            return False

        task = WarmupTask(sandbox_id=sandbox_id, owner=owner)

        try:
            self._queue.put_nowait(task)
            self._pending.add(sandbox_id)
            self._stats.enqueue_total += 1
            self._log.debug(
                "warmup_queue.enqueued",
                sandbox_id=sandbox_id,
                depth=self._queue.qsize(),
            )
            return True
        except asyncio.QueueFull:
            self._stats.drop_total += 1

            if self._stats.drop_total % max(self._config.warmup_queue_drop_alert_threshold, 1) == 0:
                self._log.warning(
                    "warmup_queue.drop_alert",
                    total_drops=self._stats.drop_total,
                    policy=self._config.warmup_queue_drop_policy,
                    sandbox_id=sandbox_id,
                )

            if self._config.warmup_queue_drop_policy == "drop_oldest":
                # Remove oldest item from queue and enqueue this one
                try:
                    evicted = self._queue.get_nowait()
                    if evicted is not None:
                        self._pending.discard(evicted.sandbox_id)
                    self._queue.put_nowait(task)
                    self._pending.add(sandbox_id)
                    self._stats.enqueue_total += 1
                    return True
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

            # drop_newest (default): just drop
            self._log.debug(
                "warmup_queue.dropped",
                sandbox_id=sandbox_id,
                policy=self._config.warmup_queue_drop_policy,
            )
            return False

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker loop: consume tasks and execute warmup."""
        self._log.info("warmup_worker.started", worker_id=worker_id)

        while self._running:
            try:
                # Wait for a task with timeout to allow checking _running flag
                try:
                    task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                # Sentinel check for shutdown
                if task is None:
                    break

                self._stats.active_workers += 1
                try:
                    await self._process_task(task, worker_id)
                finally:
                    self._stats.active_workers -= 1
                    # Remove from dedup set
                    self._pending.discard(task.sandbox_id)
                    self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.exception(
                    "warmup_worker.unexpected_error",
                    worker_id=worker_id,
                    error=str(exc),
                )

        self._log.info("warmup_worker.stopped", worker_id=worker_id)

    async def _process_task(self, task: WarmupTask, worker_id: int) -> None:
        """Process a single warmup task.

        Idempotent: checks sandbox state before calling ensure_running().
        """
        from app.api.dependencies import get_driver
        from app.db.session import get_async_session
        from app.managers.sandbox import SandboxManager
        from app.models.sandbox import Sandbox

        self._stats.consumed_total += 1

        self._log.debug(
            "warmup_worker.processing",
            worker_id=worker_id,
            sandbox_id=task.sandbox_id,
            owner=task.owner,
        )

        try:
            async with get_async_session() as db:
                # Quick state check: skip if sandbox no longer needs warmup
                from sqlmodel import select

                result = await db.execute(
                    select(Sandbox).where(
                        Sandbox.id == task.sandbox_id,
                        Sandbox.deleted_at.is_(None),
                    )
                )
                sandbox = result.scalars().first()

                if sandbox is None:
                    self._log.debug(
                        "warmup_worker.skip.deleted",
                        sandbox_id=task.sandbox_id,
                        owner=task.owner,
                    )
                    return

                self._log.info(
                    "warmup_worker.ensure_running.start",
                    worker_id=worker_id,
                    sandbox_id=sandbox.id,
                    owner=sandbox.owner,
                    profile_id=sandbox.profile_id,
                    current_session_id=sandbox.current_session_id,
                    is_warm_pool=sandbox.is_warm_pool,
                    warm_state=sandbox.warm_state,
                )

                # Execute warmup. Runtime liveness is validated inside
                # SandboxManager.ensure_running() / SessionManager.ensure_running(),
                # so we must not trust a stale DB-only RUNNING state here.
                manager = SandboxManager(driver=get_driver(), db_session=db)
                session = await manager.ensure_running(sandbox)

                self._log.info(
                    "warmup_worker.ensure_running.complete",
                    worker_id=worker_id,
                    sandbox_id=sandbox.id,
                    owner=sandbox.owner,
                    profile_id=sandbox.profile_id,
                    current_session_id=sandbox.current_session_id,
                    recovered_session_id=session.id,
                    recovered_container_id=session.container_id,
                    observed_state=session.observed_state,
                    endpoint=session.endpoint,
                    is_warm_pool=sandbox.is_warm_pool,
                    warm_state=sandbox.warm_state,
                )

                # If this is a warm pool sandbox, mark it as available after warmup
                if sandbox.is_warm_pool and sandbox.warm_state is None:
                    from app.config import get_settings

                    settings = get_settings()
                    profile = settings.get_profile(sandbox.profile_id)
                    warm_rotate_ttl = profile.warm_rotate_ttl if profile else 1800
                    await manager.mark_warm_available(
                        sandbox.id,
                        warm_rotate_ttl=warm_rotate_ttl,
                    )
                    self._log.info(
                        "warmup_worker.mark_available.complete",
                        worker_id=worker_id,
                        sandbox_id=sandbox.id,
                        profile_id=sandbox.profile_id,
                        warm_rotate_ttl=warm_rotate_ttl,
                    )

            self._stats.success_total += 1
            self._log.info(
                "warmup_worker.success",
                sandbox_id=task.sandbox_id,
                owner=task.owner,
                worker_id=worker_id,
            )

        except Exception as exc:
            self._stats.failure_total += 1
            self._log.warning(
                "warmup_worker.failed",
                sandbox_id=task.sandbox_id,
                owner=task.owner,
                worker_id=worker_id,
                error=str(exc),
            )
