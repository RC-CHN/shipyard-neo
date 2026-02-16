"""Unit tests for CargoManager.

Tests cargo CRUD operations using FakeDriver and in-memory SQLite.
Includes new tests for managed filter and delete protection.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.config import ProfileConfig, ResourceSpec, Settings
from app.errors import ConflictError, NotFoundError
from app.managers.cargo import CargoManager
from app.models.cargo import Cargo
from app.models.sandbox import Sandbox
from app.utils.datetime import utcnow
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
def cargo_manager(
    fake_driver: FakeDriver,
    db_session: AsyncSession,
    fake_settings: Settings,
) -> CargoManager:
    """Create CargoManager with FakeDriver."""
    with patch("app.managers.cargo.cargo.get_settings", return_value=fake_settings):
        manager = CargoManager(driver=fake_driver, db_session=db_session)
        yield manager


class TestCargoManagerCreate:
    """Unit tests for CargoManager.create."""

    async def test_create_external_cargo(
        self,
        cargo_manager: CargoManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Create external cargo sets managed=False."""
        # Act
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=False,
            managed_by_sandbox_id=None,
        )

        # Assert
        assert cargo is not None
        assert cargo.id.startswith("ws-")
        assert cargo.owner == "test-user"
        assert cargo.managed is False
        assert cargo.managed_by_sandbox_id is None
        assert cargo.backend == "docker_volume"

        # Assert volume was created
        assert len(fake_driver.create_volume_calls) == 1

    async def test_create_managed_cargo(
        self,
        cargo_manager: CargoManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Create managed cargo sets managed=True with sandbox reference."""
        # Act
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-123",
        )

        # Assert
        assert cargo.managed is True
        assert cargo.managed_by_sandbox_id == "sandbox-123"

    async def test_create_cargo_with_size_limit(
        self,
        cargo_manager: CargoManager,
    ):
        """Create cargo with custom size limit."""
        # Act
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=False,
            size_limit_mb=2048,
        )

        # Assert
        assert cargo.size_limit_mb == 2048


class TestCargoManagerList:
    """Unit tests for CargoManager.list with managed filter."""

    async def test_list_all_cargos(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """List all cargos without managed filter."""
        # Arrange
        await cargo_manager.create(owner="test-user", managed=False)
        await cargo_manager.create(owner="test-user", managed=True, managed_by_sandbox_id="sb-1")

        # Act
        cargos, cursor = await cargo_manager.list(owner="test-user", managed=None)

        # Assert
        assert len(cargos) == 2

    async def test_list_external_cargos_only(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """List only external cargos with managed=False."""
        # Arrange
        ext_cargo = await cargo_manager.create(owner="test-user", managed=False)
        await cargo_manager.create(owner="test-user", managed=True, managed_by_sandbox_id="sb-1")

        # Act
        cargos, cursor = await cargo_manager.list(owner="test-user", managed=False)

        # Assert
        assert len(cargos) == 1
        assert cargos[0].id == ext_cargo.id
        assert cargos[0].managed is False

    async def test_list_managed_cargos_only(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """List only managed cargos with managed=True."""
        # Arrange
        await cargo_manager.create(owner="test-user", managed=False)
        managed_cargo = await cargo_manager.create(
            owner="test-user", managed=True, managed_by_sandbox_id="sb-1"
        )

        # Act
        cargos, cursor = await cargo_manager.list(owner="test-user", managed=True)

        # Assert
        assert len(cargos) == 1
        assert cargos[0].id == managed_cargo.id
        assert cargos[0].managed is True

    async def test_list_pagination(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """List cargos with pagination."""
        # Arrange - create 5 cargos
        for _ in range(5):
            await cargo_manager.create(owner="test-user", managed=False)

        # Act - list with limit 2
        cargos, cursor = await cargo_manager.list(owner="test-user", limit=2)

        # Assert
        assert len(cargos) == 2
        assert cursor is not None

    async def test_list_respects_owner(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """List only returns cargos for the specified owner."""
        # Arrange
        await cargo_manager.create(owner="user-a", managed=False)
        await cargo_manager.create(owner="user-b", managed=False)

        # Act
        cargos, cursor = await cargo_manager.list(owner="user-a")

        # Assert
        assert len(cargos) == 1
        assert cargos[0].owner == "user-a"


class TestCargoManagerGet:
    """Unit tests for CargoManager.get."""

    async def test_get_cargo_success(
        self,
        cargo_manager: CargoManager,
    ):
        """Get cargo by ID and owner."""
        # Arrange
        created = await cargo_manager.create(owner="test-user", managed=False)

        # Act
        cargo = await cargo_manager.get(created.id, owner="test-user")

        # Assert
        assert cargo.id == created.id

    async def test_get_cargo_not_found(
        self,
        cargo_manager: CargoManager,
    ):
        """Get non-existent cargo raises NotFoundError."""
        # Act & Assert
        with pytest.raises(NotFoundError):
            await cargo_manager.get("ws-nonexistent", owner="test-user")

    async def test_get_cargo_wrong_owner(
        self,
        cargo_manager: CargoManager,
    ):
        """Get cargo with wrong owner raises NotFoundError."""
        # Arrange
        created = await cargo_manager.create(owner="user-a", managed=False)

        # Act & Assert
        with pytest.raises(NotFoundError):
            await cargo_manager.get(created.id, owner="user-b")


class TestCargoManagerDelete:
    """Unit tests for CargoManager.delete with protection logic."""

    async def test_delete_external_cargo_success(
        self,
        cargo_manager: CargoManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Delete unreferenced external cargo succeeds."""
        # Arrange
        cargo = await cargo_manager.create(owner="test-user", managed=False)
        cargo_id = cargo.id

        # Act
        await cargo_manager.delete(cargo_id, owner="test-user")

        # Assert - cargo gone
        result = await db_session.execute(select(Cargo).where(Cargo.id == cargo_id))
        assert result.scalars().first() is None

        # Assert - volume deleted
        assert len(fake_driver.delete_volume_calls) == 1

    async def test_delete_external_cargo_referenced_by_active_sandbox(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """Delete external cargo referenced by active sandbox raises ConflictError (D3)."""
        # Arrange - create external cargo
        cargo = await cargo_manager.create(owner="test-user", managed=False)

        # Create an active sandbox referencing this cargo
        sandbox = Sandbox(
            id="sandbox-test-123",
            owner="test-user",
            profile_id="python-default",
            cargo_id=cargo.id,
            deleted_at=None,  # Active sandbox
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act & Assert
        with pytest.raises(ConflictError) as exc_info:
            await cargo_manager.delete(cargo.id, owner="test-user")

        # Verify error contains active_sandbox_ids
        assert "active_sandbox_ids" in exc_info.value.details
        assert "sandbox-test-123" in exc_info.value.details["active_sandbox_ids"]

    async def test_delete_external_cargo_after_sandbox_soft_deleted(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """Delete external cargo succeeds after referencing sandbox is soft-deleted."""
        # Arrange
        cargo = await cargo_manager.create(owner="test-user", managed=False)

        # Create a soft-deleted sandbox
        sandbox = Sandbox(
            id="sandbox-deleted-123",
            owner="test-user",
            profile_id="python-default",
            cargo_id=cargo.id,
            deleted_at=utcnow(),  # Soft-deleted
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act - should not raise
        await cargo_manager.delete(cargo.id, owner="test-user")

        # Assert - cargo deleted
        result = await db_session.execute(select(Cargo).where(Cargo.id == cargo.id))
        assert result.scalars().first() is None

    async def test_delete_managed_cargo_with_active_sandbox_raises_409(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """Delete managed cargo with active managing sandbox raises ConflictError (D2)."""
        # Arrange - create managed cargo
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-active",
        )

        # Create the managing sandbox (active)
        sandbox = Sandbox(
            id="sandbox-active",
            owner="test-user",
            profile_id="python-default",
            cargo_id=cargo.id,
            deleted_at=None,  # Active
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act & Assert
        with pytest.raises(ConflictError):
            await cargo_manager.delete(cargo.id, owner="test-user")

    async def test_delete_managed_cargo_after_sandbox_soft_deleted(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """Delete managed cargo succeeds when managing sandbox is soft-deleted (D2)."""
        # Arrange
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-softdel",
        )

        # Create soft-deleted managing sandbox
        sandbox = Sandbox(
            id="sandbox-softdel",
            owner="test-user",
            profile_id="python-default",
            cargo_id=cargo.id,
            deleted_at=utcnow(),  # Soft-deleted
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act - should succeed
        await cargo_manager.delete(cargo.id, owner="test-user")

        # Assert
        result = await db_session.execute(select(Cargo).where(Cargo.id == cargo.id))
        assert result.scalars().first() is None

    async def test_delete_managed_cargo_orphan(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """Delete managed cargo with no managing sandbox (orphan) succeeds."""
        # Arrange - managed cargo with no sandbox reference
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id=None,  # Orphan
        )

        # Act - should succeed
        await cargo_manager.delete(cargo.id, owner="test-user")

        # Assert
        result = await db_session.execute(select(Cargo).where(Cargo.id == cargo.id))
        assert result.scalars().first() is None

    async def test_delete_managed_cargo_force_bypasses_check(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """Delete managed cargo with force=True bypasses all checks."""
        # Arrange
        cargo = await cargo_manager.create(
            owner="test-user",
            managed=True,
            managed_by_sandbox_id="sandbox-active",
        )

        # Create active managing sandbox
        sandbox = Sandbox(
            id="sandbox-active",
            owner="test-user",
            profile_id="python-default",
            cargo_id=cargo.id,
            deleted_at=None,
        )
        db_session.add(sandbox)
        await db_session.commit()

        # Act - force delete
        await cargo_manager.delete(cargo.id, owner="test-user", force=True)

        # Assert - deleted despite active sandbox
        result = await db_session.execute(select(Cargo).where(Cargo.id == cargo.id))
        assert result.scalars().first() is None


class TestCargoManagerDeleteInternal:
    """Unit tests for CargoManager.delete_internal_by_id."""

    async def test_delete_internal_idempotent(
        self,
        cargo_manager: CargoManager,
        db_session: AsyncSession,
    ):
        """delete_internal_by_id is idempotent - no error on missing cargo."""
        # Act - should not raise even though cargo doesn't exist
        await cargo_manager.delete_internal_by_id("ws-nonexistent")

    async def test_delete_internal_deletes_cargo(
        self,
        cargo_manager: CargoManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """delete_internal_by_id deletes cargo without owner check."""
        # Arrange
        cargo = await cargo_manager.create(owner="test-user", managed=True)
        cargo_id = cargo.id

        # Act
        await cargo_manager.delete_internal_by_id(cargo_id)

        # Assert
        result = await db_session.execute(select(Cargo).where(Cargo.id == cargo_id))
        assert result.scalars().first() is None
        assert len(fake_driver.delete_volume_calls) == 1
