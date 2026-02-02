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
from app.models.sandbox import Sandbox
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
        managed: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Workspace], str | None]:
        """List workspaces for owner.
        
        Args:
            owner: Owner identifier
            managed: Filter by managed status (None = all, True = managed only, False = external only)
            limit: Maximum number of results
            cursor: Pagination cursor
            
        Returns:
            Tuple of (workspaces, next_cursor)
        """
        query = select(Workspace).where(Workspace.owner == owner)

        # Filter by managed status if specified
        if managed is not None:
            query = query.where(Workspace.managed == managed)

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
        
        For external workspaces (managed=False):
        - Cannot delete if still referenced by active sandboxes (deleted_at IS NULL)
        - Returns 409 with active_sandbox_ids if referenced
        
        For managed workspaces (managed=True):
        - If force=True: delete unconditionally (internal cascade delete)
        - If force=False:
          - If managed_by_sandbox_id is None: allow delete (orphan workspace)
          - If managing sandbox doesn't exist or is soft-deleted: allow delete
          - Otherwise: return 409
        
        Args:
            workspace_id: Workspace ID
            owner: Owner identifier
            force: If True, skip all checks (for cascade delete)
            
        Raises:
            NotFoundError: If workspace not found
            ConflictError: If workspace still in use or managed by active sandbox
        """
        workspace = await self.get(workspace_id, owner)

        if not force:
            if not workspace.managed:
                # External workspace: check for active sandbox references
                result = await self._db.execute(
                    select(Sandbox.id).where(
                        Sandbox.workspace_id == workspace_id,
                        Sandbox.deleted_at.is_(None),
                    )
                )
                active_sandbox_ids = [row[0] for row in result.fetchall()]
                
                if active_sandbox_ids:
                    raise ConflictError(
                        f"Cannot delete workspace {workspace_id}: still referenced by active sandboxes",
                        details={"active_sandbox_ids": active_sandbox_ids},
                    )
            else:
                # Managed workspace: check if managing sandbox is still active
                if workspace.managed_by_sandbox_id is not None:
                    # Check if the managing sandbox exists and is not soft-deleted
                    result = await self._db.execute(
                        select(Sandbox).where(Sandbox.id == workspace.managed_by_sandbox_id)
                    )
                    managing_sandbox = result.scalars().first()
                    
                    if managing_sandbox is not None and managing_sandbox.deleted_at is None:
                        # Managing sandbox is still active
                        raise ConflictError(
                            f"Cannot delete managed workspace {workspace_id}: "
                            f"managing sandbox {workspace.managed_by_sandbox_id} is still active. "
                            f"Delete the sandbox instead.",
                            details={"managed_by_sandbox_id": workspace.managed_by_sandbox_id},
                        )
                # If managed_by_sandbox_id is None or sandbox is deleted, allow deletion

        self._log.info(
            "workspace.delete",
            workspace_id=workspace_id,
            volume=workspace.driver_ref,
            managed=workspace.managed,
            force=force,
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
