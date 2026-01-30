"""Unit tests for SandboxManager.

Tests sandbox lifecycle operations using FakeDriver and in-memory SQLite.
See: plans/phase-1/tests.md section 2.1-2.3
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from app.config import ProfileConfig, ResourceSpec, Settings
from app.errors import SandboxExpiredError, SandboxTTLInfiniteError, ValidationError
from app.managers.sandbox import SandboxManager
from app.models.sandbox import Sandbox, SandboxStatus
from app.models.session import Session, SessionStatus
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
def sandbox_manager(
    fake_driver: FakeDriver,
    db_session: AsyncSession,
    fake_settings: Settings,
) -> SandboxManager:
    """Create SandboxManager with FakeDriver."""
    with patch("app.managers.sandbox.sandbox.get_settings", return_value=fake_settings):
        with patch("app.managers.workspace.workspace.get_settings", return_value=fake_settings):
            manager = SandboxManager(driver=fake_driver, db_session=db_session)
            yield manager


class TestSandboxManagerCreate:
    """Unit-01: SandboxManager.create tests.
    
    Purpose: Verify sandbox creation also creates managed workspace correctly.
    """

    async def test_create_sandbox_creates_managed_workspace(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Create sandbox should create a managed workspace."""
        # Act
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
        )

        # Assert sandbox exists
        assert sandbox is not None
        assert sandbox.id.startswith("sandbox-")
        assert sandbox.owner == "test-user"
        assert sandbox.profile_id == "python-default"
        assert sandbox.workspace_id is not None
        assert sandbox.current_session_id is None  # No session created initially
        assert sandbox.deleted_at is None

        # Assert workspace was created and is managed
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == sandbox.workspace_id)
        )
        workspace = result.scalars().first()
        
        assert workspace is not None
        assert workspace.managed is True
        assert workspace.managed_by_sandbox_id == sandbox.id
        assert workspace.owner == "test-user"
        
        # Assert volume was created via driver
        assert len(fake_driver.create_volume_calls) == 1
        volume_call = fake_driver.create_volume_calls[0]
        assert volume_call["name"].startswith("bay-workspace-")

    async def test_create_sandbox_with_ttl(
        self,
        sandbox_manager: SandboxManager,
        db_session: AsyncSession,
    ):
        """Create sandbox with TTL should set expires_at."""
        # Act
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
            ttl=3600,  # 1 hour
        )

        # Assert
        assert sandbox.expires_at is not None
        # TTL should be approximately 1 hour from now
        delta = sandbox.expires_at - datetime.utcnow()
        assert 3590 < delta.total_seconds() < 3610

    async def test_create_sandbox_without_ttl_has_no_expiry(
        self,
        sandbox_manager: SandboxManager,
        db_session: AsyncSession,
    ):
        """Create sandbox without TTL should have no expiry."""
        # Act
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
            ttl=None,
        )

        # Assert
        assert sandbox.expires_at is None

    async def test_create_sandbox_status_is_idle(
        self,
        sandbox_manager: SandboxManager,
        db_session: AsyncSession,
    ):
        """Newly created sandbox should have idle status."""
        # Act
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
        )

        # Assert
        status = sandbox.compute_status(current_session=None)
        assert status == SandboxStatus.IDLE


class TestSandboxManagerStop:
    """Unit-02: SandboxManager.stop tests.
    
    Purpose: Verify stop stops session but keeps workspace.
    """

    async def test_stop_clears_current_session(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
        fake_settings: Settings,
    ):
        """Stop should clear current_session_id on sandbox."""
        # Arrange: Create sandbox with a session
        sandbox = await sandbox_manager.create(owner="test-user")
        
        # Create a session manually
        session = Session(
            id="sess-test-123",
            sandbox_id=sandbox.id,
            runtime_type="ship",
            profile_id="python-default",
            container_id="fake-container-1",
            endpoint="http://localhost:8123",
            desired_state=SessionStatus.RUNNING,
            observed_state=SessionStatus.RUNNING,
        )
        db_session.add(session)
        await db_session.commit()
        
        # Update sandbox with current session
        sandbox.current_session_id = session.id
        await db_session.commit()
        
        # Act
        await sandbox_manager.stop(sandbox)
        
        # Refresh from DB
        await db_session.refresh(sandbox)

        # Assert
        assert sandbox.current_session_id is None
        assert sandbox.idle_expires_at is None

    async def test_stop_calls_driver_stop(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Stop should call driver.stop for container."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        
        session = Session(
            id="sess-test-456",
            sandbox_id=sandbox.id,
            runtime_type="ship",
            profile_id="python-default",
            container_id="fake-container-1",
            observed_state=SessionStatus.RUNNING,
        )
        db_session.add(session)
        sandbox.current_session_id = session.id
        await db_session.commit()
        
        # Act
        await sandbox_manager.stop(sandbox)

        # Assert driver.stop was called
        assert "fake-container-1" in fake_driver.stop_calls

    async def test_stop_preserves_workspace(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Stop should NOT delete the workspace."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        workspace_id = sandbox.workspace_id
        
        # Act
        await sandbox_manager.stop(sandbox)

        # Assert workspace still exists
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = result.scalars().first()
        assert workspace is not None
        
        # Assert no delete_volume calls
        assert len(fake_driver.delete_volume_calls) == 0

    async def test_stop_is_idempotent(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Stop should be idempotent - repeated calls should not fail."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        
        # Act - call stop multiple times
        await sandbox_manager.stop(sandbox)
        await sandbox_manager.stop(sandbox)
        await sandbox_manager.stop(sandbox)

        # Assert - no error raised, sandbox state is consistent
        await db_session.refresh(sandbox)
        assert sandbox.current_session_id is None


class TestSandboxManagerDelete:
    """Unit-03: SandboxManager.delete tests.
    
    Purpose: Verify delete cascade deletes managed workspace.
    """

    async def test_delete_sets_deleted_at(
        self,
        sandbox_manager: SandboxManager,
        db_session: AsyncSession,
    ):
        """Delete should set deleted_at (soft delete)."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        sandbox_id = sandbox.id
        
        # Act
        await sandbox_manager.delete(sandbox)
        
        # Assert - sandbox has deleted_at set
        result = await db_session.execute(
            select(Sandbox).where(Sandbox.id == sandbox_id)
        )
        deleted_sandbox = result.scalars().first()
        assert deleted_sandbox is not None
        assert deleted_sandbox.deleted_at is not None

    async def test_delete_cascade_deletes_managed_workspace(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Delete should cascade delete managed workspace."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        workspace_id = sandbox.workspace_id
        
        # Get workspace driver_ref for assertion
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = result.scalars().first()
        volume_name = workspace.driver_ref
        
        # Act
        await sandbox_manager.delete(sandbox)

        # Assert - workspace record deleted
        result = await db_session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        )
        workspace = result.scalars().first()
        assert workspace is None

        # Assert - driver.delete_volume called
        assert volume_name in fake_driver.delete_volume_calls

    async def test_delete_destroys_sessions(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
    ):
        """Delete should destroy all sessions."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        
        # Create sessions
        session1 = Session(
            id="sess-1",
            sandbox_id=sandbox.id,
            container_id="container-1",
        )
        session2 = Session(
            id="sess-2",
            sandbox_id=sandbox.id,
            container_id="container-2",
        )
        db_session.add(session1)
        db_session.add(session2)
        await db_session.commit()
        
        # Act
        await sandbox_manager.delete(sandbox)

        # Assert - driver.destroy called for both containers
        assert "container-1" in fake_driver.destroy_calls
        assert "container-2" in fake_driver.destroy_calls

    async def test_delete_clears_current_session(
        self,
        sandbox_manager: SandboxManager,
        db_session: AsyncSession,
    ):
        """Delete should clear current_session_id."""
        # Arrange
        sandbox = await sandbox_manager.create(owner="test-user")
        sandbox.current_session_id = "some-session"
        await db_session.commit()
        sandbox_id = sandbox.id
        
        # Act
        await sandbox_manager.delete(sandbox)

        # Assert
        result = await db_session.execute(
            select(Sandbox).where(Sandbox.id == sandbox_id)
        )
        deleted_sandbox = result.scalars().first()
        assert deleted_sandbox.current_session_id is None


class TestSandboxManagerExtendTTL:
    """Unit-XX: SandboxManager.extend_ttl tests."""

    async def test_extend_ttl_success(self, sandbox_manager: SandboxManager):
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
            ttl=3600,
        )
        assert sandbox.expires_at is not None
        old = sandbox.expires_at

        updated = await sandbox_manager.extend_ttl(
            sandbox_id=sandbox.id,
            owner="test-user",
            extend_by=600,
        )
        assert updated.expires_at is not None
        # allow small timing drift
        assert (updated.expires_at - old).total_seconds() >= 590

    async def test_extend_ttl_rejects_infinite(self, sandbox_manager: SandboxManager):
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
            ttl=None,
        )
        assert sandbox.expires_at is None

        with pytest.raises(SandboxTTLInfiniteError):
            await sandbox_manager.extend_ttl(
                sandbox_id=sandbox.id,
                owner="test-user",
                extend_by=10,
            )

    async def test_extend_ttl_rejects_expired(self, sandbox_manager: SandboxManager):
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
            ttl=3600,
        )
        assert sandbox.expires_at is not None

        # force expires_at to past
        sandbox.expires_at = datetime.utcnow() - timedelta(seconds=5)
        await sandbox_manager._db.commit()

        with pytest.raises(SandboxExpiredError):
            await sandbox_manager.extend_ttl(
                sandbox_id=sandbox.id,
                owner="test-user",
                extend_by=10,
            )

    async def test_extend_ttl_rejects_non_positive(self, sandbox_manager: SandboxManager):
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="python-default",
            ttl=3600,
        )

        with pytest.raises(ValidationError):
            await sandbox_manager.extend_ttl(
                sandbox_id=sandbox.id,
                owner="test-user",
                extend_by=0,
            )


class TestRuntimeTypeFromProfile:
    """Unit tests for runtime_type configuration.
    
    Purpose: Verify runtime_type is correctly read from ProfileConfig.
    """

    async def test_profile_default_runtime_type_is_ship(self):
        """ProfileConfig should default runtime_type to 'ship'."""
        profile = ProfileConfig(id="test-profile")
        assert profile.runtime_type == "ship"

    async def test_profile_custom_runtime_type(self):
        """ProfileConfig should accept custom runtime_type."""
        profile = ProfileConfig(
            id="browser-profile",
            runtime_type="browser",
            image="bay-browser:latest",
        )
        assert profile.runtime_type == "browser"

    async def test_settings_profiles_have_runtime_type(
        self,
        fake_settings: Settings,
    ):
        """Settings profiles should have runtime_type field."""
        profile = fake_settings.get_profile("python-default")
        assert profile is not None
        # Default should be "ship" if not explicitly set
        assert profile.runtime_type == "ship"

    async def test_session_inherits_runtime_type_from_profile(
        self,
        sandbox_manager: SandboxManager,
        fake_driver: FakeDriver,
        db_session: AsyncSession,
        fake_settings: Settings,
    ):
        """Session should inherit runtime_type from ProfileConfig.
        
        This is the core test: when ensure_running creates a session,
        it should use profile.runtime_type instead of hardcoded 'ship'.
        """
        # Create a profile with custom runtime_type
        custom_profile = ProfileConfig(
            id="custom-runtime",
            runtime_type="custom",
            image="custom-runtime:latest",
            runtime_port=9000,
        )
        fake_settings.profiles.append(custom_profile)

        # Create sandbox with custom profile
        sandbox = await sandbox_manager.create(
            owner="test-user",
            profile_id="custom-runtime",
        )

        # Manually create a session to verify runtime_type propagation
        # (ensure_running would do this, but we test the session creation directly)
        from app.managers.session import SessionManager
        from unittest.mock import patch

        with patch("app.managers.session.session.get_settings", return_value=fake_settings):
            session_mgr = SessionManager(driver=fake_driver, db_session=db_session)
            
            # Get workspace for session creation
            workspace_result = await db_session.execute(
                select(Workspace).where(Workspace.id == sandbox.workspace_id)
            )
            workspace = workspace_result.scalars().first()

            # Create session
            session = await session_mgr.create(
                sandbox_id=sandbox.id,
                workspace=workspace,
                profile=custom_profile,
            )

            # Assert runtime_type is inherited from profile
            assert session.runtime_type == "custom"
