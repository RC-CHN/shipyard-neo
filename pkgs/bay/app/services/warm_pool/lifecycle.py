"""Warm pool lifecycle management for FastAPI lifespan integration.

Manages the startup and shutdown of:
- WarmupQueue (in-process bounded queue + workers)
- WarmPoolScheduler (periodic pool maintenance)
"""

from __future__ import annotations

import structlog
from sqlmodel import select

from app.api.dependencies import get_driver
from app.config import get_settings
from app.db.session import get_async_session
from app.managers.sandbox import SandboxManager
from app.models.sandbox import Sandbox, WarmState
from app.services.warm_pool.queue import WarmupQueue
from app.services.warm_pool.scheduler import WarmPoolScheduler

logger = structlog.get_logger()

# Global instances
_warmup_queue: WarmupQueue | None = None
_warm_pool_scheduler: WarmPoolScheduler | None = None


async def _requeue_existing_warm_pool_sandboxes(warmup_queue: WarmupQueue) -> None:
    """Requeue persisted warm sandboxes after process restart.

    Why:
    - The warmup queue is in-memory only, so queued tasks are lost on process/pod restarts.
    - Warm sandboxes persisted with `warm_state=None` would otherwise be counted as
      "pending" forever, preventing the scheduler from replenishing the pool.
    - Previously available warm sandboxes may also have lost their backing runtime
      after a cluster restart, so they should be revalidated via ensure_running().
    """
    async with get_async_session() as db:
        result = await db.execute(
            select(Sandbox).where(
                Sandbox.deleted_at.is_(None),
                Sandbox.is_warm_pool.is_(True),
            )
        )
        warm_sandboxes = result.scalars().all()

    if not warm_sandboxes:
        logger.info("warm_pool.reconcile.complete", total=0, requeued=0, skipped_retiring=0)
        return

    requeued = 0
    skipped_retiring = 0
    skipped_enqueue = 0

    for sandbox in warm_sandboxes:
        if sandbox.warm_state == WarmState.RETIRING.value:
            skipped_retiring += 1
            continue

        if warmup_queue.enqueue(sandbox_id=sandbox.id, owner=sandbox.owner):
            requeued += 1
        else:
            skipped_enqueue += 1

    logger.info(
        "warm_pool.reconcile.complete",
        total=len(warm_sandboxes),
        requeued=requeued,
        skipped_retiring=skipped_retiring,
        skipped_enqueue=skipped_enqueue,
    )


async def init_warm_pool() -> tuple[WarmupQueue | None, WarmPoolScheduler | None]:
    """Initialize warm pool services.

    Called during FastAPI lifespan startup, after database initialization.

    Returns:
        Tuple of (WarmupQueue, WarmPoolScheduler) instances
    """
    global _warmup_queue, _warm_pool_scheduler

    settings = get_settings()
    warm_config = settings.warm_pool

    if not warm_config.enabled:
        logger.info("warm_pool.disabled")
        return None, None

    # Check if any profile has warm pool enabled
    has_warm_profiles = any(p.warm_pool_size > 0 for p in settings.profiles)

    logger.info(
        "warm_pool.init",
        enabled=warm_config.enabled,
        has_warm_profiles=has_warm_profiles,
        workers=warm_config.warmup_queue_workers,
        queue_size=warm_config.warmup_queue_max_size,
    )

    # Always start the warmup queue (used by both create endpoint and pool scheduler)
    _warmup_queue = WarmupQueue(config=warm_config)
    await _warmup_queue.start()

    if has_warm_profiles:
        await _requeue_existing_warm_pool_sandboxes(_warmup_queue)

        # Start pool scheduler only if there are profiles with warm pool
        _warm_pool_scheduler = WarmPoolScheduler(
            config=warm_config,
            warmup_queue=_warmup_queue,
        )

        # Run once on startup if configured
        if warm_config.run_on_startup:
            logger.info("warm_pool.run_on_startup.start")
            try:
                results = await _warm_pool_scheduler.run_once()
                total_created = sum(results.values())
                logger.info(
                    "warm_pool.run_on_startup.complete",
                    created=total_created,
                    profiles=results,
                )
            except Exception as e:
                logger.exception("warm_pool.run_on_startup.failed", error=str(e))

        await _warm_pool_scheduler.start()

    return _warmup_queue, _warm_pool_scheduler


async def _cleanup_warm_pool_sandboxes_on_shutdown() -> None:
    """Best-effort cleanup of warm pool sandboxes during process shutdown.

    Why:
    - Warm pool sandboxes are process-managed resources.
    - On graceful shutdown, proactively stopping/deleting them avoids leftover
      warm containers/volumes when the instance is intentionally terminating.

    Safety:
    - Only targets `is_warm_pool=True` records.
    - Claimed/user sandboxes (`is_warm_pool=False`) are never touched.
    - Best-effort: failures are logged and do not block shutdown completion.
    """
    settings = get_settings()
    if not settings.warm_pool.enabled:
        return

    try:
        async with get_async_session() as db:
            result = await db.execute(
                select(Sandbox.id, Sandbox.owner).where(
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                )
            )
            warm_sandbox_refs = [(row[0], row[1]) for row in result.all()]

            if not warm_sandbox_refs:
                return

            manager = SandboxManager(driver=get_driver(), db_session=db)
            deleted = 0
            for sandbox_id, owner in warm_sandbox_refs:
                try:
                    sandbox = await manager.get(sandbox_id, owner)
                    await manager.delete(
                        sandbox,
                        delete_source="warm_pool.lifecycle.shutdown_cleanup",
                    )
                    deleted += 1
                except Exception as exc:
                    logger.warning(
                        "warm_pool.shutdown_cleanup.delete_failed",
                        sandbox_id=sandbox_id,
                        error=str(exc),
                    )

            logger.info(
                "warm_pool.shutdown_cleanup.complete",
                total=len(warm_sandbox_refs),
                deleted=deleted,
            )
    except Exception as exc:
        logger.warning(
            "warm_pool.shutdown_cleanup.failed",
            error=str(exc),
        )


async def shutdown_warm_pool() -> None:
    """Stop warm pool services gracefully.

    Called during FastAPI lifespan shutdown.
    """
    global _warmup_queue, _warm_pool_scheduler

    if _warm_pool_scheduler is not None:
        await _warm_pool_scheduler.stop()
        _warm_pool_scheduler = None

    if _warmup_queue is not None:
        await _warmup_queue.stop()
        _warmup_queue = None

    # After producers/consumers are stopped, clean up residual warm pool sandboxes.
    await _cleanup_warm_pool_sandboxes_on_shutdown()


def get_warmup_queue() -> WarmupQueue | None:
    """Get the global warmup queue instance."""
    return _warmup_queue


def get_warm_pool_scheduler() -> WarmPoolScheduler | None:
    """Get the global warm pool scheduler instance."""
    return _warm_pool_scheduler
