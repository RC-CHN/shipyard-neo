"""IdleSessionGC - Reclaim compute for sandboxes that have been idle beyond idle_timeout."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.concurrency.locks import get_sandbox_lock
from app.managers.session import SessionManager
from app.models.sandbox import Sandbox
from app.models.session import Session
from app.services.gc.base import GCResult, GCTask
from app.utils.datetime import utcnow

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()


class IdleSessionGC(GCTask):
    """GC task for reclaiming compute from idle sandboxes.

    Trigger condition:
        sandbox.idle_expires_at < now AND sandbox.deleted_at IS NULL

    Action:
        1. Acquire sandbox lock
        2. Double-check idle_expires_at < now (user may have activated)
        3. Destroy all sessions
        4. Clear sandbox.current_session_id and sandbox.idle_expires_at
    """

    def __init__(
        self,
        driver: "Driver",
        db_session: AsyncSession,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(gc_task="idle_session")
        self._session_mgr = SessionManager(driver, db_session)

    @property
    def name(self) -> str:
        return "idle_session"

    async def run(self) -> GCResult:
        """Execute idle session cleanup."""
        result = GCResult(task_name=self.name)
        now = utcnow()

        # Start a fresh transaction to see latest committed data.
        # This is safe because:
        # 1. GC tasks run sequentially (not concurrently)
        # 2. Previous task always commits before next task runs
        # 3. This task hasn't made any changes yet at this point
        # Without this, SQLite may serve stale data from a long-lived transaction.
        await self._db.rollback()

        # Find sandboxes with expired idle timeout
        query = select(Sandbox).where(
            Sandbox.deleted_at.is_(None),
            Sandbox.idle_expires_at.is_not(None),
            Sandbox.idle_expires_at < now,
        )

        db_result = await self._db.execute(query)
        sandboxes = db_result.scalars().all()

        # Extract sandbox IDs upfront to avoid lazy loading issues after rollback.
        # After _process_sandbox calls rollback, the sandbox objects become detached
        # and accessing their attributes would trigger lazy loading in wrong context.
        sandbox_ids = [sandbox.id for sandbox in sandboxes]

        self._log.info(
            "gc.idle_session.found",
            count=len(sandbox_ids),
        )

        for sandbox_id in sandbox_ids:
            try:
                cleaned = await self._process_sandbox(sandbox_id)
                if cleaned:
                    result.cleaned_count += 1
                else:
                    result.skipped_count += 1
            except Exception as e:
                self._log.exception(
                    "gc.idle_session.item_error",
                    sandbox_id=sandbox_id,
                    error=str(e),
                )
                result.add_error(f"sandbox {sandbox_id}: {e}")

        return result

    async def _process_sandbox(self, sandbox_id: str) -> bool:
        """Process a single sandbox. Returns True if cleaned, False if skipped."""
        lock = await get_sandbox_lock(sandbox_id)
        async with lock:
            # Rollback and refetch to get fresh state
            await self._db.rollback()

            query = select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()
            db_result = await self._db.execute(query)
            sandbox = db_result.scalars().first()

            if sandbox is None or sandbox.deleted_at is not None:
                self._log.debug(
                    "gc.idle_session.skip.deleted",
                    sandbox_id=sandbox_id,
                )
                return False

            # Double-check: user may have activated the sandbox while we waited for lock
            now = utcnow()
            if sandbox.idle_expires_at is None or sandbox.idle_expires_at >= now:
                self._log.debug(
                    "gc.idle_session.skip.still_active",
                    sandbox_id=sandbox_id,
                    idle_expires_at=sandbox.idle_expires_at,
                )
                return False

            self._log.info(
                "gc.idle_session.cleaning",
                sandbox_id=sandbox_id,
                idle_expires_at=sandbox.idle_expires_at.isoformat(),
            )

            # Destroy all sessions for this sandbox
            sessions_result = await self._db.execute(
                select(Session).where(Session.sandbox_id == sandbox_id)
            )
            sessions = sessions_result.scalars().all()

            for session in sessions:
                try:
                    await self._session_mgr.destroy(session)
                except Exception as e:
                    # Log but continue with other sessions
                    self._log.warning(
                        "gc.idle_session.session_destroy_error",
                        session_id=session.id,
                        error=str(e),
                    )

            # Clear sandbox session state
            sandbox.current_session_id = None
            sandbox.idle_expires_at = None
            await self._db.commit()

            self._log.info(
                "gc.idle_session.cleaned",
                sandbox_id=sandbox_id,
                sessions_destroyed=len(sessions),
            )

            return True
