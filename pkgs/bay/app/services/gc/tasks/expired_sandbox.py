"""ExpiredSandboxGC - Delete sandboxes that have exceeded their TTL."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.concurrency.locks import cleanup_sandbox_lock, get_sandbox_lock
from app.managers.session import SessionManager
from app.managers.workspace import WorkspaceManager
from app.models.sandbox import Sandbox
from app.models.session import Session
from app.services.gc.base import GCResult, GCTask

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()


class ExpiredSandboxGC(GCTask):
    """GC task for deleting sandboxes past their TTL.

    Trigger condition:
        sandbox.expires_at < now AND sandbox.deleted_at IS NULL

    Action:
        1. Acquire sandbox lock
        2. Double-check expires_at < now (user may have extended TTL)
        3. Destroy all sessions
        4. Soft-delete sandbox (set deleted_at)
        5. Cascade delete managed workspace
    """

    def __init__(
        self,
        driver: "Driver",
        db_session: AsyncSession,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(gc_task="expired_sandbox")
        self._session_mgr = SessionManager(driver, db_session)
        self._workspace_mgr = WorkspaceManager(driver, db_session)

    @property
    def name(self) -> str:
        return "expired_sandbox"

    async def run(self) -> GCResult:
        """Execute expired sandbox cleanup."""
        result = GCResult(task_name=self.name)
        now = datetime.utcnow()

        # Start a fresh transaction to see latest committed data.
        # This is safe because:
        # 1. GC tasks run sequentially (not concurrently)
        # 2. Previous task always commits before next task runs
        # 3. This task hasn't made any changes yet at this point
        # Without this, SQLite may serve stale data from a long-lived transaction.
        await self._db.rollback()

        # Find sandboxes with expired TTL
        query = select(Sandbox).where(
            Sandbox.deleted_at.is_(None),
            Sandbox.expires_at.is_not(None),
            Sandbox.expires_at < now,
        )

        db_result = await self._db.execute(query)
        sandboxes = db_result.scalars().all()

        # Extract needed attributes upfront to avoid lazy loading issues after rollback.
        # After _process_sandbox calls rollback, the sandbox objects become detached
        # and accessing their attributes would trigger lazy loading in wrong context.
        sandbox_data = [
            (sandbox.id, sandbox.owner, sandbox.workspace_id)
            for sandbox in sandboxes
        ]

        self._log.info(
            "gc.expired_sandbox.found",
            count=len(sandbox_data),
        )

        for sandbox_id, owner, workspace_id in sandbox_data:
            try:
                cleaned = await self._process_sandbox(sandbox_id, owner, workspace_id)
                if cleaned:
                    result.cleaned_count += 1
                else:
                    result.skipped_count += 1
            except Exception as e:
                self._log.exception(
                    "gc.expired_sandbox.item_error",
                    sandbox_id=sandbox_id,
                    error=str(e),
                )
                result.add_error(f"sandbox {sandbox_id}: {e}")

        return result

    async def _process_sandbox(
        self, sandbox_id: str, owner: str, workspace_id: str
    ) -> bool:
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
                    "gc.expired_sandbox.skip.deleted",
                    sandbox_id=sandbox_id,
                )
                return False

            # Double-check: user may have extended TTL while we waited for lock
            now = datetime.utcnow()
            if sandbox.expires_at is None or sandbox.expires_at >= now:
                self._log.debug(
                    "gc.expired_sandbox.skip.ttl_extended",
                    sandbox_id=sandbox_id,
                    expires_at=sandbox.expires_at,
                )
                return False

            self._log.info(
                "gc.expired_sandbox.deleting",
                sandbox_id=sandbox_id,
                expires_at=sandbox.expires_at.isoformat(),
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
                    self._log.warning(
                        "gc.expired_sandbox.session_destroy_error",
                        session_id=session.id,
                        error=str(e),
                    )

            # Get workspace for cascade delete
            workspace = await self._workspace_mgr.get_by_id(workspace_id)

            # Soft delete sandbox
            sandbox.deleted_at = datetime.utcnow()
            sandbox.current_session_id = None
            await self._db.commit()

            # Cascade delete managed workspace
            if workspace and workspace.managed:
                try:
                    await self._workspace_mgr.delete(
                        workspace.id,
                        owner,
                        force=True,
                    )
                except Exception as e:
                    self._log.warning(
                        "gc.expired_sandbox.workspace_delete_error",
                        workspace_id=workspace.id,
                        error=str(e),
                    )

            self._log.info(
                "gc.expired_sandbox.deleted",
                sandbox_id=sandbox_id,
                sessions_destroyed=len(sessions),
            )

        # Cleanup lock outside of lock context
        await cleanup_sandbox_lock(sandbox_id)

        return True
