"""SandboxManager - manages sandbox lifecycle.

Sandbox is the external-facing resource that aggregates
Cargo + Profile + Session(s).

See: plans/bay-design.md section 2.4
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
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
from app.managers.cargo import CargoManager
from app.managers.session import SessionManager
from app.models.cargo import Cargo
from app.models.sandbox import Sandbox, SandboxStatus, WarmState
from app.models.session import Session
from app.utils.datetime import utcnow

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SandboxListItem:
    sandbox: Sandbox
    status: SandboxStatus


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
        self._cargo_mgr = CargoManager(driver, db_session)
        self._session_mgr = SessionManager(driver, db_session)

    async def create(
        self,
        owner: str,
        *,
        profile_id: str = "python-default",
        cargo_id: str | None = None,
        ttl: int | None = None,
    ) -> Sandbox:
        """Create a new sandbox.

        Args:
            owner: Owner identifier
            profile_id: Profile ID
            cargo_id: Optional existing cargo ID
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

        # Create or get cargo
        if cargo_id:
            # Use existing external cargo
            cargo = await self._cargo_mgr.get(cargo_id, owner)
        else:
            # Create managed cargo
            cargo = await self._cargo_mgr.create(
                owner=owner,
                managed=True,
                managed_by_sandbox_id=sandbox_id,
            )

        # Calculate expiry
        expires_at = None
        if ttl and ttl > 0:
            expires_at = utcnow() + timedelta(seconds=ttl)

        # Create sandbox
        sandbox = Sandbox(
            id=sandbox_id,
            owner=owner,
            profile_id=profile_id,
            cargo_id=cargo.id,
            expires_at=expires_at,
            created_at=utcnow(),
            last_active_at=utcnow(),
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

    async def get_any(self, sandbox_id: str, owner: str) -> Sandbox:
        """Get sandbox by ID, including soft-deleted rows.

        Args:
            sandbox_id: Sandbox ID
            owner: Owner identifier

        Returns:
            Sandbox if found (including soft-deleted)

        Raises:
            NotFoundError: If sandbox not found
        """
        result = await self._db.execute(
            select(Sandbox).where(
                Sandbox.id == sandbox_id,
                Sandbox.owner == owner,
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
    ) -> tuple[list[SandboxListItem], str | None]:
        """List sandboxes for owner.

        Args:
            owner: Owner identifier
            status: Optional status filter
            limit: Maximum number of results
            cursor: Pagination cursor

        Returns:
            Tuple of (sandboxes, next_cursor)
        """
        now = utcnow()
        scan_cursor = cursor

        # Limit per-call scanning so rare filters don't force unbounded work.
        scan_batch_size = min(max(limit * 5, 50), 500)
        max_scanned = max(limit * 20, 1000)

        returned: list[SandboxListItem] = []
        last_scanned_id: str | None = None
        scanned = 0

        while scanned < max_scanned:
            query = select(Sandbox).where(
                Sandbox.owner == owner,
                Sandbox.deleted_at.is_(None),
                Sandbox.is_warm_pool.is_(False),  # Exclude warm pool sandboxes
            )

            if scan_cursor:
                query = query.where(Sandbox.id > scan_cursor)

            query = query.order_by(Sandbox.id).limit(scan_batch_size)

            result = await self._db.execute(query)
            batch = list(result.scalars().all())
            if not batch:
                return returned, None

            scanned += len(batch)
            last_scanned_id = batch[-1].id

            session_ids = [
                sandbox.current_session_id
                for sandbox in batch
                if sandbox.current_session_id is not None
            ]
            sessions_by_id: dict[str, Session] = {}
            if session_ids:
                sessions_result = await self._db.execute(
                    select(Session).where(Session.id.in_(session_ids))
                )
                sessions_by_id = {s.id: s for s in sessions_result.scalars().all()}

            for sandbox in batch:
                current_session = (
                    sessions_by_id.get(sandbox.current_session_id)
                    if sandbox.current_session_id is not None
                    else None
                )
                computed_status = sandbox.compute_status(
                    now=now,
                    current_session=current_session,
                )
                if status is None or computed_status == status:
                    returned.append(SandboxListItem(sandbox=sandbox, status=computed_status))
                    if len(returned) >= limit:
                        # Cursor is the last scanned sandbox_id at the point we reached the limit.
                        next_cursor = sandbox.id
                        # Match CargoManager cursor semantics:
                        # only return cursor if there may be more.
                        has_more_result = await self._db.execute(
                            select(Sandbox.id)
                            .where(
                                Sandbox.owner == owner,
                                Sandbox.deleted_at.is_(None),
                                Sandbox.is_warm_pool.is_(False),
                                Sandbox.id > next_cursor,
                            )
                            .order_by(Sandbox.id)
                            .limit(1)
                        )
                        has_more = has_more_result.first() is not None
                        return returned[:limit], next_cursor if has_more else None

            scan_cursor = last_scanned_id
            if len(batch) < scan_batch_size:
                # Exhausted.
                return returned, None

        # Scanned limit reached; return what we found so far, plus a cursor to continue scanning.
        return returned, last_scanned_id

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

        # Get sandbox_id and cargo_id before acquiring lock (avoid lazy loading issues inside lock)
        sandbox_id = sandbox.id
        cargo_id = sandbox.cargo_id

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
                select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()
            )
            locked_sandbox = result.scalars().first()
            if locked_sandbox is None:
                raise NotFoundError(f"Sandbox not found: {sandbox_id}")

            # Re-fetch cargo after rollback (objects are expired after rollback)
            cargo = await self._cargo_mgr.get_by_id(cargo_id)
            if cargo is None:
                raise NotFoundError(f"Cargo not found: {cargo_id}")

            # Check if we have a current session (re-check after acquiring lock)
            session = None
            if locked_sandbox.current_session_id:
                session = await self._session_mgr.get(locked_sandbox.current_session_id)

            # Create session if needed
            if session is None:
                session = await self._session_mgr.create(
                    sandbox_id=locked_sandbox.id,
                    cargo=cargo,
                    profile=profile,
                )
                locked_sandbox.current_session_id = session.id
                await self._db.commit()

            # Ensure session is running
            session = await self._session_mgr.ensure_running(
                session=session,
                cargo=cargo,
                profile=profile,
            )

            # Update idle timeout
            locked_sandbox.idle_expires_at = utcnow() + timedelta(seconds=profile.idle_timeout)
            locked_sandbox.last_active_at = utcnow()
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

        Uses in-memory lock + SELECT FOR UPDATE to prevent stale reads.
        """
        if extend_by <= 0:
            raise ValidationError("extend_by must be a positive integer")

        # Use same per-sandbox lock as ensure_running to prevent concurrent
        # modifications and stale reads under SQLite.
        sandbox_lock = await get_sandbox_lock(sandbox_id)
        async with sandbox_lock:
            # Start a fresh transaction to see latest committed data.
            # This is safe because no writes have occurred yet in this method.
            # Without this, SQLite may serve stale expires_at from a long-lived transaction.
            await self._db.rollback()

            # Use SELECT FOR UPDATE for PostgreSQL; in-memory lock covers SQLite.
            result = await self._db.execute(
                select(Sandbox)
                .where(
                    Sandbox.id == sandbox_id,
                    Sandbox.owner == owner,
                    Sandbox.deleted_at.is_(None),
                )
                .with_for_update()
            )
            sandbox = result.scalars().first()

            if sandbox is None:
                raise NotFoundError(f"Sandbox not found: {sandbox_id}")

            old = sandbox.expires_at
            if old is None:
                raise SandboxTTLInfiniteError(
                    details={
                        "sandbox_id": sandbox_id,
                    }
                )

            now = utcnow()
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
            sandbox.idle_expires_at = utcnow() + timedelta(seconds=profile.idle_timeout)

        sandbox.last_active_at = utcnow()
        await self._db.commit()

    async def stop(self, sandbox: Sandbox) -> None:
        """Stop sandbox - reclaim compute, keep cargo.

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
                select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()
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

    async def delete(
        self,
        sandbox: Sandbox,
        *,
        delete_source: str = "unspecified",
        request_id: str | None = None,
    ) -> None:
        """Delete sandbox permanently.

        - Destroys all sessions
        - Cascade deletes managed cargo
        - Does NOT cascade delete external cargo
        Uses same lock as ensure_running to prevent race conditions.

        Args:
            sandbox: Sandbox to delete
            delete_source: Caller/source tag for observability
            request_id: Correlated request ID for tracing
        """
        sandbox_id = sandbox.id
        owner = sandbox.owner
        cargo_id = sandbox.cargo_id
        self._log.info(
            "sandbox.delete",
            sandbox_id=sandbox_id,
            owner=owner,
            delete_source=delete_source,
            request_id=request_id,
        )

        # Use same lock as ensure_running to prevent race conditions with GC
        sandbox_lock = await get_sandbox_lock(sandbox_id)
        async with sandbox_lock:
            # Rollback and refetch to get fresh state
            await self._db.rollback()

            result = await self._db.execute(
                select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()
            )
            locked_sandbox = result.scalars().first()

            if locked_sandbox is None or locked_sandbox.deleted_at is not None:
                # Already deleted, nothing to do
                self._log.info(
                    "sandbox.delete.noop",
                    sandbox_id=sandbox_id,
                    owner=owner,
                    delete_source=delete_source,
                    request_id=request_id,
                    reason="already_deleted_or_missing",
                )
                return

            # Destroy all sessions
            result = await self._db.execute(
                select(Session).where(Session.sandbox_id == locked_sandbox.id)
            )
            sessions = result.scalars().all()

            for session in sessions:
                await self._session_mgr.destroy(session)

            # Get cargo (re-fetch after rollback)
            cargo = await self._cargo_mgr.get_by_id(cargo_id)

            # Soft delete sandbox
            locked_sandbox.deleted_at = utcnow()
            locked_sandbox.current_session_id = None
            await self._db.commit()

            self._log.info(
                "sandbox.delete.soft_deleted",
                sandbox_id=sandbox_id,
                owner=owner,
                delete_source=delete_source,
                request_id=request_id,
            )

            # Cascade delete managed cargo
            if cargo and cargo.managed:
                await self._cargo_mgr.delete(
                    cargo.id,
                    owner,
                    force=True,  # Allow deleting managed workspace
                )

                self._log.info(
                    "sandbox.delete.cascade_cargo_deleted",
                    sandbox_id=sandbox_id,
                    owner=owner,
                    cargo_id=cargo.id,
                    delete_source=delete_source,
                    request_id=request_id,
                )

        # Cleanup in-memory lock for this sandbox (outside of lock)
        await cleanup_sandbox_lock(sandbox_id)

    async def delete_by_id(
        self,
        *,
        sandbox_id: str,
        owner: str,
        idempotent: bool = True,
        delete_source: str = "unspecified",
        request_id: str | None = None,
    ) -> None:
        """Delete sandbox by ID with optional idempotent semantics.

        Args:
            sandbox_id: Sandbox ID to delete
            owner: Owner identifier
            idempotent: If True, deleting an already soft-deleted sandbox is a no-op
            delete_source: Caller/source tag for observability
            request_id: Correlated request ID for tracing

        Raises:
            NotFoundError: If sandbox does not exist for owner; or if idempotent=False
                and sandbox is already soft-deleted
        """
        sandbox = await self.get_any(sandbox_id, owner)

        if sandbox.deleted_at is not None:
            if idempotent:
                self._log.info(
                    "sandbox.delete_by_id.idempotent_noop",
                    sandbox_id=sandbox_id,
                    owner=owner,
                    delete_source=delete_source,
                    request_id=request_id,
                )
                return
            raise NotFoundError(f"Sandbox not found: {sandbox_id}")

        await self.delete(
            sandbox,
            delete_source=delete_source,
            request_id=request_id,
        )

    # ==================== Warm Pool Methods ====================

    async def claim_warm_sandbox(
        self,
        owner: str,
        profile_id: str,
        ttl: int | None = None,
    ) -> Sandbox | None:
        """Attempt to claim a warm sandbox atomically.

        Uses "short transaction + conditional update" for SQLite compatibility
        (no SELECT ... FOR UPDATE / SKIP LOCKED).

        Args:
            owner: Owner identifier for the claimed sandbox
            profile_id: Profile ID to match
            ttl: Time-to-live in seconds (None/0 = no expiry)

        Returns:
            Claimed sandbox if successful, None if no warm sandbox available
        """
        max_attempts = 3  # Retry a few times in case of concurrent claim conflict
        now = utcnow()

        for attempt in range(max_attempts):
            # 1. Find a candidate warm sandbox
            result = await self._db.execute(
                select(Sandbox)
                .where(
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                    Sandbox.warm_state == WarmState.AVAILABLE.value,
                    Sandbox.profile_id == profile_id,
                )
                .order_by(Sandbox.warm_ready_at.asc())
                .limit(1)
            )
            candidate = result.scalars().first()

            if candidate is None:
                return None

            candidate_id = candidate.id

            # 2. Atomic conditional update (claim)
            from sqlalchemy import update

            stmt = (
                update(Sandbox)
                .where(
                    Sandbox.id == candidate_id,
                    Sandbox.deleted_at.is_(None),
                    Sandbox.is_warm_pool.is_(True),
                    Sandbox.profile_id == profile_id,
                    Sandbox.warm_state == WarmState.AVAILABLE.value,
                )
                .values(
                    warm_state=WarmState.CLAIMED.value,
                    warm_claimed_at=now,
                    is_warm_pool=False,
                    owner=owner,
                    last_active_at=now,
                    expires_at=(now + timedelta(seconds=ttl) if ttl and ttl > 0 else None),
                )
            )
            update_result = await self._db.execute(stmt)
            await self._db.commit()

            if update_result.rowcount == 1:
                # Claim succeeded - refetch the sandbox
                result = await self._db.execute(
                    select(Sandbox).where(
                        Sandbox.id == candidate_id,
                        Sandbox.deleted_at.is_(None),
                    )
                )
                claimed = result.scalars().first()
                if claimed is None:
                    self._log.warning(
                        "sandbox.warm_claim.postcheck_missing",
                        sandbox_id=candidate_id,
                        owner=owner,
                        profile_id=profile_id,
                        attempt=attempt + 1,
                    )
                    await self._db.rollback()
                    continue

                # Transfer managed cargo ownership to the claiming user.
                # Otherwise /v1/cargos?managed=true cannot see claimed warm cargos.
                cargo_result = await self._db.execute(
                    select(Cargo).where(Cargo.id == claimed.cargo_id)
                )
                cargo = cargo_result.scalars().first()
                if cargo is not None:
                    cargo.owner = owner
                    await self._db.commit()
                self._log.info(
                    "sandbox.warm_claim.success",
                    sandbox_id=candidate_id,
                    owner=owner,
                    profile_id=profile_id,
                    attempt=attempt + 1,
                    cargo_id=claimed.cargo_id,
                )
                return claimed

            # Claim failed (concurrent claim), retry
            self._log.debug(
                "sandbox.warm_claim.conflict",
                sandbox_id=candidate_id,
                attempt=attempt + 1,
            )
            await self._db.rollback()

        # All attempts failed
        self._log.info(
            "sandbox.warm_claim.exhausted",
            owner=owner,
            profile_id=profile_id,
        )
        return None

    async def create_warm_sandbox(
        self,
        profile_id: str,
        warm_rotate_ttl: int = 1800,
        owner: str = "warm-pool",
    ) -> Sandbox:
        """Create a warm sandbox for the pool.

        Creates a sandbox marked as warm pool with 'available' state
        (will become available after warmup completes).

        Args:
            profile_id: Profile ID for the warm sandbox
            warm_rotate_ttl: Seconds until rotation
            owner: Owner scope (default "warm-pool" for global pool)

        Returns:
            Created warm sandbox (warm_state initially None, set to 'available'
            after successful warmup by the queue worker callback)
        """
        sandbox_id = f"sandbox-{uuid.uuid4().hex[:12]}"

        # Validate profile
        profile = self._settings.get_profile(profile_id)
        if profile is None:
            raise ValidationError(f"Invalid profile: {profile_id}")

        self._log.info(
            "sandbox.create_warm",
            sandbox_id=sandbox_id,
            profile_id=profile_id,
        )

        # Create managed cargo
        cargo = await self._cargo_mgr.create(
            owner=owner,
            managed=True,
            managed_by_sandbox_id=sandbox_id,
        )

        now = utcnow()

        # Create warm sandbox
        sandbox = Sandbox(
            id=sandbox_id,
            owner=owner,
            profile_id=profile_id,
            cargo_id=cargo.id,
            expires_at=None,  # Warm pool instances don't use user TTL
            created_at=now,
            last_active_at=now,
            # Warm pool metadata
            is_warm_pool=True,
            warm_state=None,  # Will be set to 'available' after warmup
            warm_source_profile_id=profile_id,
        )

        self._db.add(sandbox)
        await self._db.commit()
        await self._db.refresh(sandbox)

        return sandbox

    async def mark_warm_available(self, sandbox_id: str, warm_rotate_ttl: int = 1800) -> None:
        """Mark a warm sandbox as available (warmup completed).

        Called by the warmup queue worker after successful ensure_running().

        Args:
            sandbox_id: Sandbox ID to mark
            warm_rotate_ttl: Seconds until rotation
        """
        now = utcnow()
        result = await self._db.execute(
            select(Sandbox).where(
                Sandbox.id == sandbox_id,
                Sandbox.is_warm_pool.is_(True),
            )
        )
        sandbox = result.scalars().first()

        if sandbox is None:
            self._log.warning(
                "sandbox.mark_warm_available.not_found",
                sandbox_id=sandbox_id,
            )
            return

        sandbox.warm_state = WarmState.AVAILABLE.value
        sandbox.warm_ready_at = now
        sandbox.warm_rotate_at = now + timedelta(seconds=warm_rotate_ttl)
        await self._db.commit()

        self._log.info(
            "sandbox.warm_available",
            sandbox_id=sandbox_id,
            warm_rotate_at=sandbox.warm_rotate_at.isoformat(),
        )

    async def mark_warm_retiring(self, sandbox_id: str) -> None:
        """Mark a warm sandbox as retiring (prevent it from being claimed).

        Args:
            sandbox_id: Sandbox ID to mark
        """
        result = await self._db.execute(
            select(Sandbox).where(
                Sandbox.id == sandbox_id,
                Sandbox.is_warm_pool.is_(True),
                Sandbox.warm_state == WarmState.AVAILABLE.value,
            )
        )
        sandbox = result.scalars().first()

        if sandbox is None:
            return

        sandbox.warm_state = WarmState.RETIRING.value
        await self._db.commit()

        self._log.info(
            "sandbox.warm_retiring",
            sandbox_id=sandbox_id,
        )
