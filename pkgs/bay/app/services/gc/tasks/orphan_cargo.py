"""OrphanWorkspaceGC - Clean up orphan managed workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.managers.workspace import WorkspaceManager
from app.models.sandbox import Sandbox
from app.models.workspace import Workspace
from app.services.gc.base import GCResult, GCTask

if TYPE_CHECKING:
    from app.drivers.base import Driver

logger = structlog.get_logger()


class OrphanWorkspaceGC(GCTask):
    """GC task for cleaning up orphan managed workspaces.

    Trigger condition:
        workspace.managed = True AND (
            workspace.managed_by_sandbox_id IS NULL OR
            sandbox.deleted_at IS NOT NULL
        )

    Action:
        Delete workspace via WorkspaceManager.delete_internal_by_id()
    """

    def __init__(
        self,
        driver: "Driver",
        db_session: AsyncSession,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(gc_task="orphan_workspace")
        self._workspace_mgr = WorkspaceManager(driver, db_session)

    @property
    def name(self) -> str:
        return "orphan_workspace"

    async def run(self) -> GCResult:
        """Execute orphan workspace cleanup."""
        result = GCResult(task_name=self.name)

        # Find orphan managed workspaces
        # Case 1: managed_by_sandbox_id is NULL
        # Case 2: referenced sandbox is soft-deleted
        orphans = await self._find_orphans()

        self._log.info(
            "gc.orphan_workspace.found",
            count=len(orphans),
        )

        for workspace_id in orphans:
            try:
                await self._workspace_mgr.delete_internal_by_id(workspace_id)
                result.cleaned_count += 1
                self._log.info(
                    "gc.orphan_workspace.deleted",
                    workspace_id=workspace_id,
                )
            except Exception as e:
                self._log.exception(
                    "gc.orphan_workspace.item_error",
                    workspace_id=workspace_id,
                    error=str(e),
                )
                result.add_error(f"workspace {workspace_id}: {e}")

        return result

    async def _find_orphans(self) -> list[str]:
        """Find orphan managed workspace IDs."""
        orphan_ids: list[str] = []

        # Case 1: managed=True but managed_by_sandbox_id is NULL
        query1 = select(Workspace.id).where(
            Workspace.managed == True,  # noqa: E712
            Workspace.managed_by_sandbox_id.is_(None),
        )
        result1 = await self._db.execute(query1)
        for (workspace_id,) in result1:
            orphan_ids.append(workspace_id)

        # Case 2: managed=True and referenced sandbox is soft-deleted
        # Use LEFT OUTER JOIN to find workspaces where sandbox.deleted_at IS NOT NULL
        SandboxAlias = aliased(Sandbox)
        query2 = (
            select(Workspace.id)
            .outerjoin(
                SandboxAlias,
                Workspace.managed_by_sandbox_id == SandboxAlias.id,
            )
            .where(
                Workspace.managed == True,  # noqa: E712
                Workspace.managed_by_sandbox_id.is_not(None),
                SandboxAlias.deleted_at.is_not(None),
            )
        )
        result2 = await self._db.execute(query2)
        for (workspace_id,) in result2:
            if workspace_id not in orphan_ids:
                orphan_ids.append(workspace_id)

        return orphan_ids
