"""SandboxManager - manages sandbox lifecycle.

Sandbox is the external-facing resource that aggregates
Workspace + Profile + Session(s).

See: plans/bay-design.md section 2.4
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.concurrency.locks import cleanup_sandbox_lock, get_sandbox_lock
from app.config import get_settings
from app.errors import (
    NotFoundError,
    SandboxExpiredError,
    SandboxTTLInfiniteError,
    ValidationError,
)
from app.managers.session import SessionManager
from app.managers.workspace import WorkspaceManager
from app.models.sandbox import Sandbox, SandboxStatus
from app.models.session import Session

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()


class SandboxManager:
    """Manages sandbox lifecycle."""

    def __init__(
        self,
        driver: "Driver",
        db_session: AsyncSession,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(manager="sandbox")
        self._settings = get_settings()

        # Sub-managers
        self._workspace_mgr = WorkspaceManager(driver, db_session)
        self._session_mgr = SessionManager(driver, db_session)

    async def create(
        self,
        owner: str,
        *,
        profile_id: str = "python-default",
        workspace_id: str | None = None,
        ttl: int | None = None,
    ) -> Sandbox:
        """Create a new sandbox.
        
        Args:
            owner: Owner identifier
            profile_id: Profile ID
            workspace_id: Optional existing workspace ID
            ttl: Time-to-live in seconds (None/0 = no expiry)
            
        Returns:
            Created sandbox
        """
        sandbox_id = f"sandbox-{uuid.uuid4().hex[:12]}"

        # Validate profile
        profile = self._settings.get_profile(profile_id)
        if profile is None:
            raise ValidationError(f"Invalid profile: {profile_id}")

        self._log.info(
            "sandbox.create",
            sandbox_id=sandbox_id,
            owner=owner,
            profile_id=profile_id,
        )

        # Create or get workspace
        if workspace_id:
            # Use existing external workspace
            workspace = await self._workspace_mgr.get(workspace_id, owner)
        else:
            # Create managed workspace
            workspace = await self._workspace_mgr.create(
                owner=owner,
                managed=True,
                managed_by_sandbox_id=sandbox_id,
            )

        # Calculate expiry
        expires_at = None
        if ttl and ttl > 0:
            expires_at = datetime.utcnow() + timedelta(seconds=ttl)

        # Create sandbox
        sandbox = Sandbox(
            id=sandbox_id,
            owner=owner,
            profile_id=profile_id,
            workspace_id=workspace.id,
            expires_at=expires_at,
            created_at=datetime.utcnow(),
            last_active_at=datetime.utcnow(),
        )

        self._db.add(sandbox)
        await self._db.commit()
        await self._db.refresh(sandbox)

        return sandbox

    async def get(self, sandbox_id: str, owner: str) -> Sandbox:
        """Get sandbox by ID.
        
        Args:
            sandbox_id: Sandbox ID
            owner: Owner identifier
            
        Returns:
            Sandbox if found and not deleted
            
        Raises:
            NotFoundError: If sandbox not found or deleted
        """
        result = await self._db.execute(
            select(Sandbox).where(
                Sandbox.id == sandbox_id,
                Sandbox.owner == owner,
                Sandbox.deleted_at.is_(None),  # Not soft-deleted
            )
        )
        sandbox = result.scalars().first()

        if sandbox is None:
            raise NotFoundError(f"Sandbox not found: {sandbox_id}")

        return sandbox

    async def list(
        self,
        owner: str,
        *,
        status: SandboxStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Sandbox], str | None]:
        """List sandboxes for owner.
        
        Args:
            owner: Owner identifier
            status: Optional status filter
            limit: Maximum number of results
            cursor: Pagination cursor
            
        Returns:
            Tuple of (sandboxes, next_cursor)
        """
        query = select(Sandbox).where(
            Sandbox.owner == owner,
            Sandbox.deleted_at.is_(None),
        )

        if cursor:
            query = query.where(Sandbox.id > cursor)

        query = query.order_by(Sandbox.id).limit(limit + 1)

        result = await self._db.execute(query)
        sandboxes = list(result.scalars().all())

        next_cursor = None
        if len(sandboxes) > limit:
            sandboxes = sandboxes[:limit]
            next_cursor = sandboxes[-1].id

        return sandboxes, next_cursor

    async def ensure_running(self, sandbox: Sandbox) -> Session:
        """Ensure sandbox has a running session.
        
        Creates a new session if needed, or returns existing one.
        Uses in-memory lock + SELECT FOR UPDATE for concurrency control:
        - In-memory lock: works for single instance (SQLite, dev mode)
        - SELECT FOR UPDATE: works for multi-instance (PostgreSQL, production)
        
        Args:
            sandbox: Sandbox to ensure is running
            
        Returns:
            Running session
        """
        profile = self._settings.get_profile(sandbox.profile_id)
        if profile is None:
            raise ValidationError(f"Invalid profile: {sandbox.profile_id}")

        # Get sandbox_id and workspace_id before acquiring lock (avoid lazy loading issues inside lock)
        sandbox_id = sandbox.id
        workspace_id = sandbox.workspace_id
        
        # In-memory lock for single-instance deployments (SQLite doesn't support FOR UPDATE)
        sandbox_lock = await get_sandbox_lock(sandbox_id)
        async with sandbox_lock:
            # Rollback any pending transaction to ensure we start fresh
            # This is critical for SQLite where different sessions may have stale snapshots
            # After rollback, the next query will start a new transaction with fresh data
            await self._db.rollback()
            
            # Re-fetch sandbox from DB with fresh transaction to see committed changes
            # FOR UPDATE works in PostgreSQL/MySQL for multi-instance deployments
            result = await self._db.execute(
                select(Sandbox)
                .where(Sandbox.id == sandbox_id)
                .with_for_update()
            )
            locked_sandbox = result.scalars().first()
            if locked_sandbox is None:
                raise NotFoundError(f"Sandbox not found: {sandbox_id}")

            # Re-fetch workspace after rollback (objects are expired after rollback)
            workspace = await self._workspace_mgr.get_by_id(workspace_id)
            if workspace is None:
                raise NotFoundError(f"Workspace not found: {workspace_id}")

            # Check if we have a current session (re-check after acquiring lock)
            session = None
            if locked_sandbox.current_session_id:
                session = await self._session_mgr.get(locked_sandbox.current_session_id)

            # Create session if needed
            if session is None:
                session = await self._session_mgr.create(
                    sandbox_id=locked_sandbox.id,
                    workspace=workspace,
                    profile=profile,
                )
                locked_sandbox.current_session_id = session.id
                await self._db.commit()

            # Ensure session is running
            session = await self._session_mgr.ensure_running(
                session=session,
                workspace=workspace,
                profile=profile,
            )

            # Update idle timeout
            locked_sandbox.idle_expires_at = datetime.utcnow() + timedelta(
                seconds=profile.idle_timeout
            )
            locked_sandbox.last_active_at = datetime.utcnow()
            await self._db.commit()

            return session

    async def extend_ttl(
        self,
        sandbox_id: str,
        owner: str,
        *,
        extend_by: int,
    ) -> Sandbox:
        """Extend sandbox TTL (expires_at) by N seconds.

        Rules:
        - If expires_at is None (infinite TTL): reject (409 sandbox_ttl_infinite)
        - If expires_at < now: reject (409 sandbox_expired)
        - Else: expires_at = max(old, now) + extend_by

        Note: Phase 1 accepts lost updates under concurrent calls.
        """
        if extend_by <= 0:
            raise ValidationError("extend_by must be a positive integer")

        sandbox = await self.get(sandbox_id, owner)

        old = sandbox.expires_at
        if old is None:
            raise SandboxTTLInfiniteError(
                details={
                    "sandbox_id": sandbox_id,
                }
            )

        now = datetime.utcnow()
        if old < now:
            raise SandboxExpiredError(
                details={
                    "sandbox_id": sandbox_id,
                    "expires_at": old.isoformat(),
                }
            )

        base = old if old > now else now
        sandbox.expires_at = base + timedelta(seconds=extend_by)
        await self._db.commit()
        await self._db.refresh(sandbox)
        return sandbox

    async def get_current_session(self, sandbox: Sandbox) -> Session | None:
        """Get current session for sandbox."""
        if sandbox.current_session_id:
            return await self._session_mgr.get(sandbox.current_session_id)
        return None

    async def keepalive(self, sandbox: Sandbox) -> None:
        """Keep sandbox alive - extend idle timeout.
        
        Does NOT implicitly start compute.
        
        Args:
            sandbox: Sandbox to keep alive
        """
        self._log.info("sandbox.keepalive", sandbox_id=sandbox.id)

        profile = self._settings.get_profile(sandbox.profile_id)
        if profile:
            sandbox.idle_expires_at = datetime.utcnow() + timedelta(
                seconds=profile.idle_timeout
            )

        sandbox.last_active_at = datetime.utcnow()
        await self._db.commit()

    async def stop(self, sandbox: Sandbox) -> None:
        """Stop sandbox - reclaim compute, keep workspace.
        
        Idempotent: repeated calls maintain final state consistency.
        Uses same lock as ensure_running to prevent race conditions.
        
        Args:
            sandbox: Sandbox to stop
        """
        sandbox_id = sandbox.id
        self._log.info("sandbox.stop", sandbox_id=sandbox_id)

        # Use same lock as ensure_running to prevent race conditions with GC
        sandbox_lock = await get_sandbox_lock(sandbox_id)
        async with sandbox_lock:
            # Rollback and refetch to get fresh state
            await self._db.rollback()
            
            result = await self._db.execute(
                select(Sandbox)
                .where(Sandbox.id == sandbox_id)
                .with_for_update()
            )
            locked_sandbox = result.scalars().first()
            
            if locked_sandbox is None or locked_sandbox.deleted_at is not None:
                # Already deleted, nothing to stop
                return

            # Stop all sessions for this sandbox
            result = await self._db.execute(
                select(Session).where(Session.sandbox_id == locked_sandbox.id)
            )
            sessions = result.scalars().all()

            for session in sessions:
                await self._session_mgr.stop(session)

            # Clear current session
            locked_sandbox.current_session_id = None
            locked_sandbox.idle_expires_at = None
            await self._db.commit()

    async def delete(self, sandbox: Sandbox) -> None:
        """Delete sandbox permanently.
        
        - Destroys all sessions
        - Cascade deletes managed workspace
        - Does NOT cascade delete external workspace
        Uses same lock as ensure_running to prevent race conditions.
        
        Args:
            sandbox: Sandbox to delete
        """
        sandbox_id = sandbox.id
        owner = sandbox.owner
        workspace_id = sandbox.workspace_id
        self._log.info("sandbox.delete", sandbox_id=sandbox_id)

        # Use same lock as ensure_running to prevent race conditions with GC
        sandbox_lock = await get_sandbox_lock(sandbox_id)
        async with sandbox_lock:
            # Rollback and refetch to get fresh state
            await self._db.rollback()
            
            result = await self._db.execute(
                select(Sandbox)
                .where(Sandbox.id == sandbox_id)
                .with_for_update()
            )
            locked_sandbox = result.scalars().first()
            
            if locked_sandbox is None or locked_sandbox.deleted_at is not None:
                # Already deleted, nothing to do
                return

            # Destroy all sessions
            result = await self._db.execute(
                select(Session).where(Session.sandbox_id == locked_sandbox.id)
            )
            sessions = result.scalars().all()

            for session in sessions:
                await self._session_mgr.destroy(session)

            # Get workspace (re-fetch after rollback)
            workspace = await self._workspace_mgr.get_by_id(workspace_id)

            # Soft delete sandbox
            locked_sandbox.deleted_at = datetime.utcnow()
            locked_sandbox.current_session_id = None
            await self._db.commit()

            # Cascade delete managed workspace
            if workspace and workspace.managed:
                await self._workspace_mgr.delete(
                    workspace.id,
                    owner,
                    force=True,  # Allow deleting managed workspace
                )

        # Cleanup in-memory lock for this sandbox (outside of lock)
        await cleanup_sandbox_lock(sandbox_id)
