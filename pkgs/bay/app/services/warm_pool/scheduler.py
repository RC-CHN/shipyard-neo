"""WarmPoolScheduler - Periodic pool maintenance.

Responsibilities (from §4.5):
1. Periodically scan profiles with warm_pool_size > 0
2. Count available warm instances
3. Replenish pool to target size
4. Rotate instances past warm_rotate_at
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from sqlmodel import func, select

from app.drivers.base import ContainerStatus
from app.models.sandbox import Sandbox, WarmState
from app.models.session import Session, SessionStatus
from app.utils.datetime import utcnow

if TYPE_CHECKING:
    from app.config import WarmPoolConfig
    from app.services.warm_pool.queue import WarmupQueue

logger = structlog.get_logger()


class WarmPoolScheduler:
    """Scheduler for warm pool maintenance.

    Periodically checks each profile's warm pool and:
    - Replenishes to target size when count < warm_pool_size
    - Rotates instances when warm_rotate_at <= now
    """

    def __init__(
        self,
        config: "WarmPoolConfig",
        warmup_queue: "WarmupQueue",
    ) -> None:
        self._config = config
        self._queue = warmup_queue
        self._log = logger.bind(service="warm_pool_scheduler")

        self._running = False
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start background maintenance loop."""
        if self._running:
            self._log.warning("warm_pool_scheduler.already_running")
            return

        self._running = True
        self._task = asyncio.create_task(
            self._background_loop(),
            name="warm-pool-scheduler",
        )
        self._log.info(
            "warm_pool_scheduler.started",
            interval_seconds=self._config.interval_seconds,
        )

    async def stop(self) -> None:
        """Stop background maintenance loop gracefully."""
        if not self._running:
            return

        self._log.info("warm_pool_scheduler.stopping")
        self._running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        self._log.info("warm_pool_scheduler.stopped")

    async def run_once(self) -> dict[str, int]:
        """Execute one pool maintenance cycle.

        Returns:
            Dict of profile_id -> number of sandboxes created
        """
        async with self._run_lock:
            return await self._run_cycle()

    async def _run_cycle(self) -> dict[str, int]:
        """Internal: Execute one pool maintenance cycle."""
        from app.config import get_settings

        settings = get_settings()
        results: dict[str, int] = {}

        # Find all profiles with warm pool enabled
        warm_profiles = [p for p in settings.profiles if p.warm_pool_size > 0]

        if not warm_profiles:
            return results

        self._log.info(
            "warm_pool.cycle.start",
            profiles=len(warm_profiles),
        )

        for profile in warm_profiles:
            try:
                created = await self._maintain_profile(profile)
                results[profile.id] = created
            except Exception as exc:
                self._log.exception(
                    "warm_pool.cycle.profile_error",
                    profile_id=profile.id,
                    error=str(exc),
                )

        total_created = sum(results.values())
        self._log.info(
            "warm_pool.cycle.complete",
            total_created=total_created,
            profiles=results,
        )

        return results

    async def _reconcile_profile_runtime_state(self, profile_id: str) -> int:
        """Reconcile DB warm-pool state with actual runtime state.

        For warm sandboxes marked as available, the DB may drift from reality after
        driver/runtime restarts. This periodically re-validates those rows against
        the runtime backend and downgrades broken entries back to pending so they
        can be re-enqueued for warmup.

        Returns:
            Number of warm sandboxes requeued for recovery.
        """
        from app.api.dependencies import get_driver
        from app.db.session import get_async_session

        async with get_async_session() as db:
            result = await db.execute(
                select(Sandbox, Session)
                .outerjoin(Session, Session.id == Sandbox.current_session_id)
                .where(
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                    Sandbox.warm_state == WarmState.AVAILABLE.value,
                    Sandbox.profile_id == profile_id,
                )
            )
            available_rows = result.all()

        if not available_rows:
            return 0

        driver = get_driver()
        stale_ids: list[str] = []
        requeued = 0

        for sandbox, session in available_rows:
            runtime_ok = False

            if session is not None:
                runtime_ok = await self._is_session_runtime_healthy(driver, session)

            if runtime_ok:
                self._log.debug(
                    "warm_pool.runtime_state_healthy",
                    sandbox_id=sandbox.id,
                    profile_id=profile_id,
                    session_id=sandbox.current_session_id,
                    current_session_id=sandbox.current_session_id,
                    warm_state=sandbox.warm_state,
                )
                continue

            stale_ids.append(sandbox.id)
            enqueue_result = self._queue.enqueue(sandbox_id=sandbox.id, owner=sandbox.owner)
            if enqueue_result:
                requeued += 1

            self._log.warning(
                "warm_pool.runtime_state_drift_detected",
                sandbox_id=sandbox.id,
                profile_id=profile_id,
                session_id=sandbox.current_session_id,
                current_session_id=sandbox.current_session_id,
                warm_state=sandbox.warm_state,
                requeued=enqueue_result,
            )

        if not stale_ids:
            return 0

        async with get_async_session() as db:
            result = await db.execute(select(Sandbox).where(Sandbox.id.in_(stale_ids)))
            stale_sandboxes = result.scalars().all()

            session_ids = [
                sandbox.current_session_id
                for sandbox in stale_sandboxes
                if sandbox.current_session_id
            ]
            sessions_by_id: dict[str, Session] = {}
            if session_ids:
                session_result = await db.execute(
                    select(Session).where(Session.id.in_(session_ids))
                )
                sessions_by_id = {session.id: session for session in session_result.scalars().all()}

            for sandbox in stale_sandboxes:
                sandbox.warm_state = None
                sandbox.warm_ready_at = None
                sandbox.warm_rotate_at = None

                if sandbox.current_session_id:
                    session = sessions_by_id.get(sandbox.current_session_id)
                    if session is not None:
                        session.observed_state = SessionStatus.STOPPED
                        session.endpoint = None
                        session.last_observed_at = utcnow()

            await db.commit()

        self._log.info(
            "warm_pool.runtime_reconcile.complete",
            profile_id=profile_id,
            checked=len(available_rows),
            stale=len(stale_ids),
            requeued=requeued,
        )
        return requeued

    async def _is_session_runtime_healthy(self, driver, session: Session) -> bool:
        """Check whether a session still has a live runtime behind it."""
        if session.containers:
            expected_container_names = [c.get("name") for c in session.containers]
            expected_container_ids = [c.get("container_id") for c in session.containers]
            try:
                instances = await driver.list_runtime_instances(
                    labels={
                        "bay.session_id": session.id,
                    }
                )
            except Exception as exc:
                self._log.warning(
                    "warm_pool.runtime_probe_failed",
                    session_id=session.id,
                    mode="multi",
                    expected_container_names=expected_container_names,
                    expected_container_ids=expected_container_ids,
                    error=str(exc),
                )
                return True

            healthy = any(instance.state == ContainerStatus.RUNNING.value for instance in instances)
            self._log.debug(
                "warm_pool.runtime_probe_result",
                session_id=session.id,
                mode="multi",
                expected_container_names=expected_container_names,
                expected_container_ids=expected_container_ids,
                discovered_runtime_ids=[instance.id for instance in instances],
                discovered_runtime_states={instance.id: instance.state for instance in instances},
                healthy=healthy,
            )
            return healthy

        if session.container_id is None:
            self._log.debug(
                "warm_pool.runtime_probe_result",
                session_id=session.id,
                mode="single",
                container_id=None,
                healthy=False,
                reason="missing_container_id",
            )
            return False

        try:
            info = await driver.status(session.container_id, runtime_port=None)
        except Exception as exc:
            self._log.warning(
                "warm_pool.runtime_probe_failed",
                session_id=session.id,
                container_id=session.container_id,
                mode="single",
                error=str(exc),
            )
            return True

        healthy = info.status == ContainerStatus.RUNNING
        self._log.debug(
            "warm_pool.runtime_probe_result",
            session_id=session.id,
            mode="single",
            container_id=session.container_id,
            discovered_status=info.status.value,
            healthy=healthy,
        )
        return healthy

    async def _maintain_profile(self, profile) -> int:
        """Maintain warm pool for a single profile.

        Returns number of new warm sandboxes created.
        """
        from app.db.session import get_async_session

        now = utcnow()
        created_count = 0

        await self._reconcile_profile_runtime_state(profile.id)

        async with get_async_session() as db:
            # Count available warm sandboxes for this profile
            count_result = await db.execute(
                select(func.count(Sandbox.id)).where(
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                    Sandbox.warm_state == WarmState.AVAILABLE.value,
                    Sandbox.profile_id == profile.id,
                )
            )
            available_count = count_result.scalar() or 0

            # Count in-flight warm sandboxes (created/enqueued but not available yet)
            pending_result = await db.execute(
                select(func.count(Sandbox.id)).where(
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                    Sandbox.warm_state.is_(None),
                    Sandbox.profile_id == profile.id,
                )
            )
            pending_count = pending_result.scalar() or 0

            self._log.debug(
                "warm_pool.profile_check",
                profile_id=profile.id,
                available=available_count,
                pending=pending_count,
                target=profile.warm_pool_size,
            )

            # Handle rotation: mark instances past warm_rotate_at as retiring
            rotate_result = await db.execute(
                select(Sandbox).where(
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                    Sandbox.warm_state == WarmState.AVAILABLE.value,
                    Sandbox.profile_id == profile.id,
                    Sandbox.warm_rotate_at.is_not(None),
                    Sandbox.warm_rotate_at <= now,
                )
            )
            expired_warm = rotate_result.scalars().all()

            for sandbox in expired_warm:
                sandbox.warm_state = WarmState.RETIRING.value
                self._log.info(
                    "warm_pool.rotating",
                    sandbox_id=sandbox.id,
                    profile_id=profile.id,
                    warm_rotate_at=sandbox.warm_rotate_at.isoformat()
                    if sandbox.warm_rotate_at
                    else None,
                )

            if expired_warm:
                await db.commit()
                # Recalculate available count after rotation
                available_count -= len(expired_warm)

            # Replenish: create warm sandboxes to fill the gap
            supply_count = max(available_count, 0) + max(pending_count, 0)
            deficit = profile.warm_pool_size - supply_count
            if deficit <= 0:
                return 0

            self._log.info(
                "warm_pool.replenishing",
                profile_id=profile.id,
                deficit=deficit,
                available=available_count,
                pending=pending_count,
            )

        # Create warm sandboxes outside the counting transaction
        for _ in range(deficit):
            try:
                sandbox_id = await self._create_warm_sandbox(profile)
                if sandbox_id:
                    created_count += 1
            except Exception as exc:
                self._log.warning(
                    "warm_pool.create_failed",
                    profile_id=profile.id,
                    error=str(exc),
                )

        # Retire old instances (schedule deletion)
        for sandbox in expired_warm:
            try:
                await self._retire_warm_sandbox(sandbox.id, sandbox.owner)
            except Exception as exc:
                self._log.warning(
                    "warm_pool.retire_failed",
                    sandbox_id=sandbox.id,
                    error=str(exc),
                )

        return created_count

    async def _create_warm_sandbox(self, profile) -> str | None:
        """Create a warm sandbox and enqueue it for warmup.

        Returns sandbox_id if created, None if failed.
        """
        from app.api.dependencies import get_driver
        from app.db.session import get_async_session
        from app.managers.sandbox import SandboxManager

        async with get_async_session() as db:
            manager = SandboxManager(driver=get_driver(), db_session=db)
            sandbox = await manager.create_warm_sandbox(
                profile_id=profile.id,
                warm_rotate_ttl=profile.warm_rotate_ttl,
            )

            self._log.info(
                "warm_pool.sandbox_created",
                sandbox_id=sandbox.id,
                profile_id=profile.id,
            )

            # Enqueue warmup via the shared queue (§2.5.1: pool + create share same queue)
            self._queue.enqueue(
                sandbox_id=sandbox.id,
                owner=sandbox.owner,
            )

            return sandbox.id

    async def _retire_warm_sandbox(self, sandbox_id: str, owner: str) -> None:
        """Retire a warm sandbox (stop and delete)."""
        from app.api.dependencies import get_driver
        from app.db.session import get_async_session
        from app.managers.sandbox import SandboxManager

        async with get_async_session() as db:
            manager = SandboxManager(driver=get_driver(), db_session=db)
            try:
                sandbox = await manager.get(sandbox_id, owner)
                await manager.delete(
                    sandbox,
                    delete_source="warm_pool.scheduler.retire",
                )
                self._log.info(
                    "warm_pool.sandbox_retired",
                    sandbox_id=sandbox_id,
                )
            except Exception as exc:
                self._log.warning(
                    "warm_pool.retire_error",
                    sandbox_id=sandbox_id,
                    error=str(exc),
                )

    async def _background_loop(self) -> None:
        """Internal background loop.

        Note:
        - If run_on_startup is enabled, lifecycle already executed one cycle.
          Sleep before the first loop cycle to avoid immediate duplicate replenishment.
        """
        first_iteration = True

        while self._running:
            should_sleep = (first_iteration and self._config.run_on_startup) or (
                not first_iteration
            )
            if should_sleep:
                try:
                    await asyncio.sleep(self._config.interval_seconds)
                except asyncio.CancelledError:
                    break

            first_iteration = False

            try:
                await self.run_once()
            except Exception as exc:
                self._log.exception(
                    "warm_pool_scheduler.cycle_error",
                    error=str(exc),
                )
