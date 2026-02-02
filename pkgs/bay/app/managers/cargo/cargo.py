"""WorkspaceManager - manages workspace lifecycle and storage.

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
from app.models.workspace import Workspace

logger = structlog.get_logger()


class WorkspaceManager:
    """Manages workspace lifecycle and storage."""

    def __init__(self, driver: Driver, db_session: AsyncSession) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(manager="workspace")
        self._settings = get_settings()

    async def create(
        self,
        owner: str,
        *,
        managed: bool = True,
        managed_by_sandbox_id: str | None = None,
        size_limit_mb: int | None = None,
    ) -> Workspace:
        """Create a new workspace.
        
        Args:
            owner: Owner identifier
            managed: If True, this workspace is managed by a sandbox
            managed_by_sandbox_id: Sandbox ID that manages this workspace
            size_limit_mb: Size limit in MB (defaults to config)
            
        Returns:
            Created workspace
        """
        workspace_id = f"ws-{uuid.uuid4().hex[:12]}"
        volume_name = f"bay-workspace-{workspace_id}"

        self._log.info(
            "workspace.create",
            workspace_id=workspace_id,
            owner=owner,
            managed=managed,
        )

        # Create volume
        await self._driver.create_volume(
            name=volume_name,
            labels={
                "bay.owner": owner,
                "bay.workspace_id": workspace_id,
                "bay.managed": str(managed).lower(),
            },
        )

        # Create DB record
        workspace = Workspace(
            id=workspace_id,
            owner=owner,
            backend="docker_volume",
            driver_ref=volume_name,
            managed=managed,
            managed_by_sandbox_id=managed_by_sandbox_id,
            size_limit_mb=size_limit_mb or self._settings.workspace.default_size_limit_mb,
            created_at=datetime.utcnow(),
            last_accessed_at=datetime.utcnow(),
        )

        self._db.add(workspace)
        await self._db.commit()
        await self._db.refresh(workspace)

        return workspace

    async def get(self, workspace_id: str, owner: str) -> Workspace:
        """Get workspace by ID.
        
        Args:
            workspace_id: Workspace ID
            owner: Owner identifier (for access check)
            
        Returns:
            Workspace if found
            
        Raises:
            NotFoundError: If workspace not found or not visible
        """
        result = await self._db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.owner == owner,
            )
        )
        workspace = result.scalars().first()

        if workspace is None:
            raise NotFoundError(f"Workspace not found: {workspace_id}")

        return workspace

    async def get_by_id(self, workspace_id: str) -> Workspace | None:
        """Get workspace by ID (internal use, no owner check)."""
        result = await self._db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        return result.scalars().first()

    async def list(
        self,
        owner: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Workspace], str | None]:
        """List workspaces for owner.
        
        Args:
            owner: Owner identifier
            limit: Maximum number of results
            cursor: Pagination cursor
            
        Returns:
            Tuple of (workspaces, next_cursor)
        """
        query = select(Workspace).where(Workspace.owner == owner)

        if cursor:
            # Cursor is the last workspace_id
            query = query.where(Workspace.id > cursor)

        query = query.order_by(Workspace.id).limit(limit + 1)

        result = await self._db.execute(query)
        workspaces = list(result.scalars().all())

        next_cursor = None
        if len(workspaces) > limit:
            workspaces = workspaces[:limit]
            next_cursor = workspaces[-1].id

        return workspaces, next_cursor

    async def delete(
        self,
        workspace_id: str,
        owner: str,
        *,
        force: bool = False,
    ) -> None:
        """Delete a workspace.
        
        For managed workspaces:
        - Can only be deleted if the managing sandbox is deleted
        - Or if force=True (internal cascade delete)
        
        Args:
            workspace_id: Workspace ID
            owner: Owner identifier
            force: If True, skip managed check (for cascade delete)
            
        Raises:
            NotFoundError: If workspace not found
            ConflictError: If trying to delete a managed workspace
        """
        workspace = await self.get(workspace_id, owner)

        if workspace.managed and not force:
            raise ConflictError(
                f"Cannot delete managed workspace {workspace_id}. "
                f"Delete the managing sandbox instead."
            )

        self._log.info(
            "workspace.delete",
            workspace_id=workspace_id,
            volume=workspace.driver_ref,
        )

        # Delete volume
        await self._driver.delete_volume(workspace.driver_ref)

        # Delete DB record
        await self._db.delete(workspace)
        await self._db.commit()

    async def touch(self, workspace_id: str) -> None:
        """Update last_accessed_at timestamp."""
        result = await self._db.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = result.scalars().first()

        if workspace:
            workspace.last_accessed_at = datetime.utcnow()
            await self._db.commit()

    async def delete_internal_by_id(self, workspace_id: str) -> None:
        """Internal delete without owner check. For GC / cascade use only.

        This method is used by OrphanWorkspaceGC to clean up orphan workspaces.
        It bypasses the owner check since GC runs in a system context.

        Args:
            workspace_id: Workspace ID to delete

        Note:
            - Idempotent: returns silently if workspace doesn't exist
            - Deletes volume first, then DB record
            - If volume delete fails, DB record is preserved
        """
        workspace = await self.get_by_id(workspace_id)
        if workspace is None:
            # Already deleted, idempotent
            return

        self._log.info(
            "workspace.delete_internal",
            workspace_id=workspace_id,
            volume=workspace.driver_ref,
        )

        # Delete volume first (may fail)
        await self._driver.delete_volume(workspace.driver_ref)

        # Delete DB record
        await self._db.delete(workspace)
        await self._db.commit()
