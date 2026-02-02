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
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Cargo], str | None]:
        """List cargos for owner.

        Args:
            owner: Owner identifier
            limit: Maximum number of results
            cursor: Pagination cursor

        Returns:
            Tuple of (cargos, next_cursor)
        """
        query = select(Cargo).where(Cargo.owner == owner)

        if cursor:
            # Cursor is the last cargo_id
            query = query.where(Cargo.id > cursor)

        query = query.order_by(Cargo.id).limit(limit + 1)

        result = await self._db.execute(query)
        cargos = list(result.scalars().all())

        next_cursor = None
        if len(cargos) > limit:
            next_cursor = cargos[limit - 1].id
            cargos = cargos[:limit]

        return cargos, next_cursor

    async def delete(
        self,
        cargo_id: str,
        owner: str,
        *,
        force: bool = False,
    ) -> None:
        """Delete a cargo.

        For managed cargos:
        - Can only be deleted if the managing sandbox is deleted
        - Or if force=True (internal cascade delete)

        Args:
            cargo_id: Cargo ID
            owner: Owner identifier
            force: If True, skip managed check (for cascade delete)

        Raises:
            NotFoundError: If cargo not found
            ConflictError: If trying to delete a managed cargo
        """
        cargo = await self.get(cargo_id, owner)

        if cargo.managed and not force:
            raise ConflictError(
                f"Cannot delete managed cargo {cargo_id}. "
                f"Delete the managing sandbox instead."
            )

        self._log.info(
            "cargo.delete",
            cargo_id=cargo_id,
            volume=cargo.driver_ref,
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
