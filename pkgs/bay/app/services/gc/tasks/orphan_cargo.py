"""OrphanCargoGC - Clean up orphan managed cargos."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.managers.cargo import CargoManager
from app.models.sandbox import Sandbox
from app.models.cargo import Cargo
from app.services.gc.base import GCResult, GCTask

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()


class OrphanCargoGC(GCTask):
    """GC task for cleaning up orphan managed cargos.

    Trigger condition:
        cargo.managed = True AND (
            cargo.managed_by_sandbox_id IS NULL OR
            sandbox.deleted_at IS NOT NULL
        )

    Action:
        Delete cargo via CargoManager.delete_internal_by_id()
    """

    def __init__(
        self,
        driver: "Driver",
        db_session: AsyncSession,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(gc_task="orphan_cargo")
        self._cargo_mgr = CargoManager(driver, db_session)

    @property
    def name(self) -> str:
        return "orphan_cargo"

    async def run(self) -> GCResult:
        """Execute orphan workspace cleanup."""
        result = GCResult(task_name=self.name)

        # Find orphan managed cargos
        # Case 1: managed_by_sandbox_id is NULL
        # Case 2: referenced sandbox is soft-deleted
        orphans = await self._find_orphans()

        self._log.info(
            "gc.orphan_cargo.found",
            count=len(orphans),
        )

        for cargo_id in orphans:
            try:
                await self._cargo_mgr.delete_internal_by_id(cargo_id)
                result.cleaned_count += 1
                self._log.info(
                    "gc.orphan_cargo.deleted",
                    cargo_id=cargo_id,
                )
            except Exception as e:
                self._log.exception(
                    "gc.orphan_cargo.item_error",
                    cargo_id=cargo_id,
                    error=str(e),
                )
                result.add_error(f"cargo {cargo_id}: {e}")

        return result

    async def _find_orphans(self) -> list[str]:
        """Find orphan managed cargo IDs."""
        orphan_ids: list[str] = []

        # Case 1: managed=True but managed_by_sandbox_id is NULL
        query1 = select(Cargo.id).where(
            Cargo.managed == True,  # noqa: E712
            Cargo.managed_by_sandbox_id.is_(None),
        )
        result1 = await self._db.execute(query1)
        for (cargo_id,) in result1:
            orphan_ids.append(cargo_id)

        # Case 2: managed=True and referenced sandbox is soft-deleted
        # Use LEFT OUTER JOIN to find workspaces where sandbox.deleted_at IS NOT NULL
        SandboxAlias = aliased(Sandbox)
        query2 = (
            select(Cargo.id)
            .outerjoin(
                SandboxAlias,
                Cargo.managed_by_sandbox_id == SandboxAlias.id,
            )
            .where(
                Cargo.managed == True,  # noqa: E712
                Cargo.managed_by_sandbox_id.is_not(None),
                SandboxAlias.deleted_at.is_not(None),
            )
        )
        result2 = await self._db.execute(query2)
        for (cargo_id,) in result2:
            if cargo_id not in orphan_ids:
                orphan_ids.append(cargo_id)

        return orphan_ids
