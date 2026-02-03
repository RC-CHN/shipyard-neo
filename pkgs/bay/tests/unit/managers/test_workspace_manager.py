"""Unit tests for WorkspaceManager.

Tests workspace CRUD operations using FakeDriver and in-memory SQLite.
Includes new tests for managed filter and delete protection.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.config import ProfileConfig, ResourceSpec, Settings
from app.errors import ConflictError, NotFoundError
from app.managers.workspace import WorkspaceManager
from app.models.sandbox import Sandbox
from app.models.workspace import Workspace
from tests.fakes import FakeDriver


@pytest.fixture
def fake_settings() -> Settings:
    """Create test settings with minimal config."""
    return Settings(
        database={"url": "sqlite+aiosqlite:///:memory:"},
        driver={"type": "docker"},
        profiles=[
            ProfileConfig(
                id="python-default",
                image="ship:latest",
                resources=ResourceSpec(cpus=1.0, memory="1g"),
                capabilities=["filesystem", "shell", "ipython"],
                idle_timeout=1800,
                runtime_port=8123,
            ),
        ],
    )


@pytest.fixture
async def db_session(fake_settings: Settings):
    """Create in-memory SQLite database and session."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async_session_factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def fake_driver() -> FakeDriver:
    """Create a FakeDriver instance."""
    return FakeDriver()


@pytest.fixture
def workspace_manager(
    fake_driver: FakeDriver,
    db_session: AsyncSession,
    fake_settings: Settings,
) -> WorkspaceManager:
    """Create WorkspaceManager with FakeDriver."""
    with patch("app.managers.workspace.workspace.get_settings", return_value=fake_settings):
        manager = WorkspaceManager(driver=fake_driver, db_session=db_session)
        yield manager


class TestWorkspaceManagerCreate:
    """Unit tests for WorkspaceManager.create."""

    async def test_create_external_workspace(
        self,
        workspace_manager: WorkspaceManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Create external workspace sets managed=False."""
        # Act
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=False,
            managed_by_sandbox_id=None,
        )

        # Assert
        assert workspace is not None
        assert workspace.id.startswith("ws-")
        assert workspace.owner == "test-user"
        assert workspace.managed is False
        assert workspace.managed_by_sandbox_id is None
        assert workspace.backend == "docker_volume"
        
        # Assert volume was created
        assert len(fake_driver.create_volume_calls) == 1

    async def test_create_managed_workspace(
        self,
        workspace_manager: WorkspaceManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Create managed workspace sets managed=True with sandbox reference."""
        # Act
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-123",
        )

        # Assert
        assert workspace.managed is True
        assert workspace.managed_by_sandbox_id == "sandbox-123"

    async def test_create_workspace_with_size_limit(
        self,
        workspace_manager: WorkspaceManager,
    ):
        """Create workspace with custom size limit."""
        # Act
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=False,
            size_limit_mb=2048,
        )

        # Assert
        assert workspace.size_limit_mb == 2048


class TestWorkspaceManagerList:
    """Unit tests for WorkspaceManager.list with managed filter."""

    async def test_list_all_workspaces(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """List all workspaces without managed filter."""
        # Arrange
        await workspace_manager.create(owner="test-user", managed=False)
        await workspace_manager.create(owner="test-user", managed=True, managed_by_sandbox_id="sb-1")

        # Act
        workspaces, cursor = await workspace_manager.list(owner="test-user", managed=None)

        # Assert
        assert len(workspaces) == 2

    async def test_list_external_workspaces_only(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """List only external workspaces with managed=False."""
        # Arrange
        ext_ws = await workspace_manager.create(owner="test-user", managed=False)
        await workspace_manager.create(owner="test-user", managed=True, managed_by_sandbox_id="sb-1")

        # Act
        workspaces, cursor = await workspace_manager.list(owner="test-user", managed=False)

        # Assert
        assert len(workspaces) == 1
        assert workspaces[0].id == ext_ws.id
        assert workspaces[0].managed is False

    async def test_list_managed_workspaces_only(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """List only managed workspaces with managed=True."""
        # Arrange
        await workspace_manager.create(owner="test-user", managed=False)
        managed_ws = await workspace_manager.create(
            owner="test-user", managed=True, managed_by_sandbox_id="sb-1"
        )

        # Act
        workspaces, cursor = await workspace_manager.list(owner="test-user", managed=True)

        # Assert
        assert len(workspaces) == 1
        assert workspaces[0].id == managed_ws.id
        assert workspaces[0].managed is True

    async def test_list_pagination(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """List workspaces with pagination."""
        # Arrange - create 5 workspaces
        for _ in range(5):
            await workspace_manager.create(owner="test-user", managed=False)

        # Act - list with limit 2
        workspaces, cursor = await workspace_manager.list(owner="test-user", limit=2)

        # Assert
        assert len(workspaces) == 2
        assert cursor is not None

    async def test_list_respects_owner(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """List only returns workspaces for the specified owner."""
        # Arrange
        await workspace_manager.create(owner="user-a", managed=False)
        await workspace_manager.create(owner="user-b", managed=False)

        # Act
        workspaces, cursor = await workspace_manager.list(owner="user-a")

        # Assert
        assert len(workspaces) == 1
        assert workspaces[0].owner == "user-a"


class TestWorkspaceManagerGet:
    """Unit tests for WorkspaceManager.get."""

    async def test_get_workspace_success(
        self,
        workspace_manager: WorkspaceManager,
    ):
        """Get workspace by ID and owner."""
        # Arrange
        created = await workspace_manager.create(owner="test-user", managed=False)

        # Act
        workspace = await workspace_manager.get(created.id, owner="test-user")

        # Assert
        assert workspace.id == created.id

    async def test_get_workspace_not_found(
        self,
        workspace_manager: WorkspaceManager,
    ):
        """Get non-existent workspace raises NotFoundError."""
        # Act & Assert
        with pytest.raises(NotFoundError):
            await workspace_manager.get("ws-nonexistent", owner="test-user")

    async def test_get_workspace_wrong_owner(
        self,
        workspace_manager: WorkspaceManager,
    ):
        """Get workspace with wrong owner raises NotFoundError."""
        # Arrange
        created = await workspace_manager.create(owner="user-a", managed=False)

        # Act & Assert
        with pytest.raises(NotFoundError):
            await workspace_manager.get(created.id, owner="user-b")


class TestWorkspaceManagerDelete:
    """Unit tests for WorkspaceManager.delete with protection logic."""

    async def test_delete_external_workspace_success(
        self,
        workspace_manager: WorkspaceManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Delete unreferenced external workspace succeeds."""
        # Arrange
        workspace = await workspace_manager.create(owner="test-user", managed=False)
        workspace_id = workspace.id

        # Act
        await workspace_manager.delete(workspace_id, owner="test-user")

        # Assert - workspace gone
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        assert result.scalars().first() is None

        # Assert - volume deleted
        assert len(fake_driver.delete_volume_calls) == 1

    async def test_delete_external_workspace_referenced_by_active_sandbox(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """Delete external workspace referenced by active sandbox raises ConflictError (D3)."""
        # Arrange - create external workspace
        workspace = await workspace_manager.create(owner="test-user", managed=False)
        
        # Create an active sandbox referencing this workspace
        sandbox = Sandbox(
            id="sandbox-test-123",
            owner="test-user",
            profile_id="python-default",
            workspace_id=workspace.id,
            deleted_at=None,  # Active sandbox
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act & Assert
        with pytest.raises(ConflictError) as exc_info:
            await workspace_manager.delete(workspace.id, owner="test-user")
        
        # Verify error contains active_sandbox_ids
        assert "active_sandbox_ids" in exc_info.value.details
        assert "sandbox-test-123" in exc_info.value.details["active_sandbox_ids"]

    async def test_delete_external_workspace_after_sandbox_soft_deleted(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """Delete external workspace succeeds after referencing sandbox is soft-deleted."""
        # Arrange
        workspace = await workspace_manager.create(owner="test-user", managed=False)
        
        # Create a soft-deleted sandbox
        sandbox = Sandbox(
            id="sandbox-deleted-123",
            owner="test-user",
            profile_id="python-default",
            workspace_id=workspace.id,
            deleted_at=datetime.utcnow(),  # Soft-deleted
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act - should not raise
        await workspace_manager.delete(workspace.id, owner="test-user")

        # Assert - workspace deleted
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace.id)
        )
        assert result.scalars().first() is None

    async def test_delete_managed_workspace_with_active_sandbox_raises_409(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """Delete managed workspace with active managing sandbox raises ConflictError (D2)."""
        # Arrange - create managed workspace
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-active",
        )
        
        # Create the managing sandbox (active)
        sandbox = Sandbox(
            id="sandbox-active",
            owner="test-user",
            profile_id="python-default",
            workspace_id=workspace.id,
            deleted_at=None,  # Active
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act & Assert
        with pytest.raises(ConflictError):
            await workspace_manager.delete(workspace.id, owner="test-user")

    async def test_delete_managed_workspace_after_sandbox_soft_deleted(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """Delete managed workspace succeeds when managing sandbox is soft-deleted (D2)."""
        # Arrange
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-softdel",
        )
        
        # Create soft-deleted managing sandbox
        sandbox = Sandbox(
            id="sandbox-softdel",
            owner="test-user",
            profile_id="python-default",
            workspace_id=workspace.id,
            deleted_at=datetime.utcnow(),  # Soft-deleted
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act - should succeed
        await workspace_manager.delete(workspace.id, owner="test-user")

        # Assert
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace.id)
        )
        assert result.scalars().first() is None

    async def test_delete_managed_workspace_orphan(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """Delete managed workspace with no managing sandbox (orphan) succeeds."""
        # Arrange - managed workspace with no sandbox reference
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id=None,  # Orphan
        )

        # Act - should succeed
        await workspace_manager.delete(workspace.id, owner="test-user")

        # Assert
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace.id)
        )
        assert result.scalars().first() is None

    async def test_delete_managed_workspace_force_bypasses_check(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """Delete managed workspace with force=True bypasses all checks."""
        # Arrange
        workspace = await workspace_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-active",
        )
        
        # Create active managing sandbox
        sandbox = Sandbox(
            id="sandbox-active",
            owner="test-user",
            profile_id="python-default",
            workspace_id=workspace.id,
            deleted_at=None,
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act - force delete
        await workspace_manager.delete(workspace.id, owner="test-user", force=True)

        # Assert - deleted despite active sandbox
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace.id)
        )
        assert result.scalars().first() is None


class TestWorkspaceManagerDeleteInternal:
    """Unit tests for WorkspaceManager.delete_internal_by_id."""

    async def test_delete_internal_idempotent(
        self,
        workspace_manager: WorkspaceManager,
        db_session: AsyncSession,
    ):
        """delete_internal_by_id is idempotent - no error on missing workspace."""
        # Act - should not raise even though workspace doesn't exist
        await workspace_manager.delete_internal_by_id("ws-nonexistent")

    async def test_delete_internal_deletes_workspace(
        self,
        workspace_manager: WorkspaceManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """delete_internal_by_id deletes workspace without owner check."""
        # Arrange
        workspace = await workspace_manager.create(owner="test-user", managed=True)
        workspace_id = workspace.id

        # Act
        await workspace_manager.delete_internal_by_id(workspace_id)

        # Assert
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        assert result.scalars().first() is None
        assert len(fake_driver.delete_volume_calls) == 1
