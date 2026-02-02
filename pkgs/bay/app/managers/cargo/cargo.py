"""CargoManager - manages cargo lifecycle and storage.

See: plans/bay-design.md section 3.2
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import get_settings
from app.drivers.base import Driver
from app.errors import ConflictError, NotFoundError
from app.models.sandbox import Sandbox
from app.models.cargo import Cargo

logger = structlog.get_logger()


class CargoManager:
    """Manages cargo lifecycle and storage."""

    def __init__(self, driver: Driver, db_session: AsyncSession) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(manager="cargo")
        self._settings = get_settings()

    async def create(
        self,
        owner: str,
        *,
        managed: bool = True,
        managed_by_sandbox_id: str | None = None,
        size_limit_mb: int | None = None,
    ) -> Cargo:
        """Create a new cargo.

        Args:
            owner: Owner identifier
            managed: If True, this cargo is managed by a sandbox
            managed_by_sandbox_id: Sandbox ID that manages this cargo
            size_limit_mb: Size limit in MB (defaults to config)

        Returns:
            Created cargo
        """
        cargo_id = f"ws-{uuid.uuid4().hex[:12]}"
        volume_name = f"bay-cargo-{cargo_id}"

        self._log.info(
            "cargo.create",
            cargo_id=cargo_id,
            owner=owner,
            managed=managed,
        )

        # Create volume
        await self._driver.create_volume(
            name=volume_name,
            labels={
                "bay.owner": owner,
                "bay.cargo_id": cargo_id,
                "bay.managed": str(managed).lower(),
            },
        )

        # Create DB record
        cargo = Cargo(
            id=cargo_id,
            owner=owner,
            backend="docker_volume",
            driver_ref=volume_name,
            managed=managed,
            managed_by_sandbox_id=managed_by_sandbox_id,
            size_limit_mb=size_limit_mb or self._settings.cargo.default_size_limit_mb,
            created_at=datetime.utcnow(),
            last_accessed_at=datetime.utcnow(),
        )

        self._db.add(cargo)
        await self._db.commit()
        await self._db.refresh(cargo)

        return cargo

    async def get(self, cargo_id: str, owner: str) -> Cargo:
        """Get cargo by ID.

        Args:
            cargo_id: Cargo ID
            owner: Owner identifier (for access check)

        Returns:
            Cargo if found

        Raises:
            NotFoundError: If cargo not found or not visible
        """
        result = await self._db.execute(
            select(Cargo).where(
                Cargo.id == cargo_id,
                Cargo.owner == owner,
            )
        )
        cargo = result.scalars().first()

        if cargo is None:
            raise NotFoundError(f"Cargo not found: {cargo_id}")

        return cargo

    async def get_by_id(self, cargo_id: str) -> Cargo | None:
        """Get cargo by ID (internal use, no owner check)."""
        result = await self._db.execute(
            select(Cargo).where(Cargo.id == cargo_id)
        )
        return result.scalars().first()

    async def list(
        self,
        owner: str,
        *,
        managed: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Cargo], str | None]:
        """List cargos for owner.

        Args:
            owner: Owner identifier
            managed: Filter by managed status (None = all, True = managed only, False = external only)
            limit: Maximum number of results
            cursor: Pagination cursor

        Returns:
            Tuple of (cargos, next_cursor)
        """
        query = select(Cargo).where(Cargo.owner == owner)

        # Filter by managed status if specified
        if managed is not None:
            query = query.where(Cargo.managed == managed)

        if cursor:
            # Cursor is the last cargo_id
            query = query.where(Cargo.id > cursor)

        query = query.order_by(Cargo.id).limit(limit + 1)

        result = await self._db.execute(query)
        cargos = list(result.scalars().all())

        next_cursor = None
        if len(cargos) > limit:
            cargos = cargos[:limit]
            next_cursor = cargos[-1].id

        return cargos, next_cursor

    async def delete(
        self,
        cargo_id: str,
        owner: str,
        *,
        force: bool = False,
    ) -> None:
        """Delete a cargo.
        
        For external cargos (managed=false):
        - Cannot delete if still referenced by active sandboxes (deleted_at IS NULL)
        - Returns 409 with active_sandbox_ids if referenced

        For managed cargos (managed=true):
        - If force=true: delete unconditionally (internal cascade delete)
        - If force=false:
          - If managed_by_sandbox_id is None: allow delete (orphan cargo)
          - If managing sandbox doesn't exist or is soft-deleted: allow delete
          - Otherwise: return 409
        
        Args:
            cargo_id: Cargo ID
            owner: Owner identifier
            force: If True, skip all checks (for cascade delete)
            
        Raises:
            NotFoundError: If cargo not found
            ConflictError: If cargo still in use or managed by active sandbox
        """
        cargo = await self.get(cargo_id, owner)

        if not force:
            if not cargo.managed:
                # External cargo: check for active sandbox references
                result = await self._db.execute(
                    select(Sandbox.id).where(
                        Sandbox.cargo_id == cargo_id,
                        Sandbox.deleted_at.is_(None),
                    )
                )
                active_sandbox_ids = [row[0] for row in result.fetchall()]
                
                if active_sandbox_ids:
                    raise ConflictError(
                        f"Cannot delete cargo {cargo_id}: still referenced by active sandboxes",
                        details={"active_sandbox_ids": active_sandbox_ids},
                    )
            else:
                # Managed cargo: check if managing sandbox is still active
                if cargo.managed_by_sandbox_id is not None:
                    # Check if the managing sandbox exists and is not soft-deleted
                    result = await self._db.execute(
                        select(Sandbox).where(Sandbox.id == cargo.managed_by_sandbox_id)
                    )
                    managing_sandbox = result.scalars().first()
                    
                    if managing_sandbox is not None and managing_sandbox.deleted_at is None:
                        # Managing sandbox is still active
                        raise ConflictError(
                            f"Cannot delete managed cargo {cargo_id}: "
                            f"managing sandbox {cargo.managed_by_sandbox_id} is still active. "
                            f"Delete the sandbox instead.",
                            details={"managed_by_sandbox_id": cargo.managed_by_sandbox_id},
                        )
                # If managed_by_sandbox_id is None or sandbox is deleted, allow deletion

        self._log.info(
            "cargo.delete",
            cargo_id=cargo_id,
            volume=cargo.driver_ref,
            managed=cargo.managed,
            force=force,
        )

        # Delete volume
        await self._driver.delete_volume(cargo.driver_ref)

        # Delete DB record
        await self._db.delete(cargo)
        await self._db.commit()

    async def touch(self, cargo_id: str) -> None:
        """Update last_accessed_at timestamp."""
        result = await self._db.execute(
            select(Cargo).where(Cargo.id == cargo_id)
        )
        cargo = result.scalars().first()

        if cargo:
            cargo.last_accessed_at = datetime.utcnow()
            await self._db.commit()

    async def delete_internal_by_id(self, cargo_id: str) -> None:
        """Internal delete without owner check. For GC / cascade use only.

        This method is used by OrphanCargoGC to clean up orphan cargos.
        It bypasses the owner check since GC runs in a system context.

        Args:
            cargo_id: Cargo ID to delete

        Note:
            - Idempotent: returns silently if cargo doesn't exist
            - Deletes volume first, then DB record
            - If volume delete fails, DB record is preserved
        """
        cargo = await self.get_by_id(cargo_id)
        if cargo is None:
            # Already deleted, idempotent
            return

        self._log.info(
            "cargo.delete_internal",
            cargo_id=cargo_id,
            volume=cargo.driver_ref,
        )

        # Delete volume first (may fail)
        await self._driver.delete_volume(cargo.driver_ref)

        # Delete DB record
        await self._db.delete(cargo)
        await self._db.commit()
