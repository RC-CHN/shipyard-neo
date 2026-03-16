"""Unit tests for SandboxManager warm pool methods."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from app.drivers.base import ContainerInfo, ContainerStatus, RuntimeInstance
from app.managers.sandbox import SandboxManager
from app.models.sandbox import Sandbox, WarmState
from app.models.session import Session, SessionStatus
from app.services.warm_pool.scheduler import WarmPoolScheduler
from app.utils.datetime import utcnow
from tests.fakes import FakeDriver


@pytest.fixture
async def db_session():
    """Create test database session with in-memory SQLite."""
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
def driver():
    return FakeDriver()


@pytest.fixture
def sandbox_mgr(driver, db_session):
    return SandboxManager(driver=driver, db_session=db_session)


class TestCreateWarmSandbox:
    """Tests for create_warm_sandbox."""

    @pytest.mark.asyncio
    async def test_create_warm_sandbox_basic(self, sandbox_mgr, db_session):
        """Should create a sandbox with warm pool metadata."""
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
            warm_rotate_ttl=3600,
        )

        assert sandbox.is_warm_pool is True
        assert sandbox.warm_state is None  # Not yet available
        assert sandbox.warm_source_profile_id == "python-default"
        assert sandbox.owner == "warm-pool"
        assert sandbox.expires_at is None  # Warm pool instances don't expire via TTL
        assert sandbox.cargo_id is not None

    @pytest.mark.asyncio
    async def test_create_warm_sandbox_custom_owner(self, sandbox_mgr):
        """Should support custom owner scope."""
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
            owner="custom-owner",
        )

        assert sandbox.owner == "custom-owner"


class TestMarkWarmAvailable:
    """Tests for mark_warm_available."""

    @pytest.mark.asyncio
    async def test_mark_available_after_warmup(self, sandbox_mgr, db_session):
        """After warmup, sandbox should be marked available with deterministic rotation time."""
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
            warm_rotate_ttl=1800,
        )

        fixed_now = utcnow()
        with patch("app.managers.sandbox.sandbox.utcnow", return_value=fixed_now):
            await sandbox_mgr.mark_warm_available(sandbox.id, warm_rotate_ttl=1800)

        # Refetch
        from sqlmodel import select

        result = await db_session.execute(select(Sandbox).where(Sandbox.id == sandbox.id))
        updated = result.scalars().first()

        assert updated.warm_state == WarmState.AVAILABLE.value
        assert updated.warm_ready_at == fixed_now
        assert updated.warm_rotate_at == fixed_now + timedelta(seconds=1800)


class TestClaimWarmSandbox:
    """Tests for claim_warm_sandbox."""

    @pytest.mark.asyncio
    async def test_claim_success(self, sandbox_mgr, db_session):
        """Should successfully claim an available warm sandbox."""
        # Create and mark available
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
        )
        await sandbox_mgr.mark_warm_available(sandbox.id, warm_rotate_ttl=1800)

        # Claim
        claimed = await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-default",
            ttl=300,
        )

        assert claimed is not None
        assert claimed.id == sandbox.id
        assert claimed.owner == "user-1"
        assert claimed.is_warm_pool is False
        assert claimed.warm_state == WarmState.CLAIMED.value
        assert claimed.warm_claimed_at is not None
        assert claimed.expires_at is not None

    @pytest.mark.asyncio
    async def test_claim_no_available(self, sandbox_mgr):
        """Should return None when no warm sandbox available."""
        claimed = await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-default",
        )

        assert claimed is None

    @pytest.mark.asyncio
    async def test_claim_wrong_profile(self, sandbox_mgr):
        """Should return None when profile doesn't match."""
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
        )
        await sandbox_mgr.mark_warm_available(sandbox.id)

        claimed = await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-data",  # Different profile
        )

        assert claimed is None

    @pytest.mark.asyncio
    async def test_claim_retiring_not_claimable(self, sandbox_mgr, db_session):
        """Retiring sandbox should not be claimable."""
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
        )
        await sandbox_mgr.mark_warm_available(sandbox.id)
        await sandbox_mgr.mark_warm_retiring(sandbox.id)

        claimed = await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-default",
        )

        assert claimed is None

    @pytest.mark.asyncio
    async def test_claim_with_no_ttl(self, sandbox_mgr):
        """Claimed sandbox with no TTL should have expires_at=None."""
        sandbox = await sandbox_mgr.create_warm_sandbox(
            profile_id="python-default",
        )
        await sandbox_mgr.mark_warm_available(sandbox.id)

        claimed = await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-default",
            ttl=None,
        )

        assert claimed is not None
        assert claimed.expires_at is None

    @pytest.mark.asyncio
    async def test_claim_picks_oldest_ready(self, sandbox_mgr, db_session):
        """Should prefer the oldest warm_ready_at sandbox."""
        # Create two warm sandboxes
        sb1 = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")
        sb2 = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")

        base = utcnow()
        with patch(
            "app.managers.sandbox.sandbox.utcnow",
            side_effect=[base, base + timedelta(milliseconds=10)],
        ):
            await sandbox_mgr.mark_warm_available(sb1.id)
            await sandbox_mgr.mark_warm_available(sb2.id)

        # Claim should pick sb1 (oldest)
        claimed = await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-default",
        )

        assert claimed is not None
        assert claimed.id == sb1.id


class TestMarkWarmRetiring:
    """Tests for mark_warm_retiring."""

    @pytest.mark.asyncio
    async def test_mark_retiring(self, sandbox_mgr, db_session):
        """Should mark available sandbox as retiring."""
        sandbox = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")
        await sandbox_mgr.mark_warm_available(sandbox.id)

        await sandbox_mgr.mark_warm_retiring(sandbox.id)

        from sqlmodel import select

        result = await db_session.execute(select(Sandbox).where(Sandbox.id == sandbox.id))
        updated = result.scalars().first()
        assert updated.warm_state == WarmState.RETIRING.value

    @pytest.mark.asyncio
    async def test_mark_retiring_already_claimed_noop(self, sandbox_mgr, db_session):
        """Should be a no-op if sandbox is already claimed."""
        sandbox = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")
        await sandbox_mgr.mark_warm_available(sandbox.id)

        # Claim it
        await sandbox_mgr.claim_warm_sandbox(
            owner="user-1",
            profile_id="python-default",
        )

        # Try to retire (should be no-op since it's claimed and no longer warm_pool)
        await sandbox_mgr.mark_warm_retiring(sandbox.id)

        # Verify state is still claimed
        from sqlmodel import select

        result = await db_session.execute(select(Sandbox).where(Sandbox.id == sandbox.id))
        updated = result.scalars().first()
        assert updated.warm_state == WarmState.CLAIMED.value


class TestListExcludesWarmPool:
    """Tests that list() excludes warm pool sandboxes."""

    @pytest.mark.asyncio
    async def test_list_excludes_warm_sandboxes(self, sandbox_mgr, db_session):
        """Warm pool sandboxes should not appear in user list."""
        # Create a normal sandbox
        normal = await sandbox_mgr.create(owner="user-1", profile_id="python-default")

        # Create a warm sandbox
        warm = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")
        await sandbox_mgr.mark_warm_available(warm.id)

        # List for the warm-pool owner
        items, _ = await sandbox_mgr.list(owner="warm-pool")
        warm_ids = {item.sandbox.id for item in items}
        assert warm.id not in warm_ids

        # List for user-1 should show normal sandbox
        items, _ = await sandbox_mgr.list(owner="user-1")
        ids = {item.sandbox.id for item in items}
        assert normal.id in ids


class _FakeWarmupQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    def enqueue(self, *, sandbox_id: str, owner: str) -> bool:
        self.enqueued.append((sandbox_id, owner))
        return True


class TestWarmPoolSchedulerReconcile:
    """Tests for periodic runtime/database reconciliation in warm pool."""

    @pytest.mark.asyncio
    async def test_reconcile_requeues_available_sandbox_when_runtime_missing(
        self,
        db_session,
        driver,
        monkeypatch,
    ):
        sandbox_mgr = SandboxManager(driver=driver, db_session=db_session)
        sandbox = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")
        await sandbox_mgr.mark_warm_available(sandbox.id)

        session = Session(
            id="sess-warm-missing",
            sandbox_id=sandbox.id,
            profile_id="python-default",
            container_id="missing-container",
            endpoint="http://dead-runtime",
            observed_state=SessionStatus.RUNNING,
            desired_state=SessionStatus.RUNNING,
        )
        db_session.add(session)
        sandbox.current_session_id = session.id
        await db_session.commit()

        queue = _FakeWarmupQueue()
        scheduler = WarmPoolScheduler(
            config=SimpleNamespace(interval_seconds=60, run_on_startup=False),
            warmup_queue=queue,
        )

        driver.set_status_override(
            "missing-container",
            ContainerInfo(
                container_id="missing-container",
                status=ContainerStatus.NOT_FOUND,
            ),
        )

        class _SessionFactory:
            async def __aenter__(self):
                return db_session

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            "app.db.session.get_async_session",
            lambda: _SessionFactory(),
        )
        monkeypatch.setattr("app.api.dependencies.get_driver", lambda: driver)

        reconciled = await scheduler._reconcile_profile_runtime_state("python-default")
        assert reconciled == 1
        assert queue.enqueued == [(sandbox.id, sandbox.owner)]

        from sqlmodel import select

        sandbox_result = await db_session.execute(select(Sandbox).where(Sandbox.id == sandbox.id))
        updated_sandbox = sandbox_result.scalars().first()
        assert updated_sandbox is not None
        assert updated_sandbox.warm_state is None
        assert updated_sandbox.warm_ready_at is None
        assert updated_sandbox.warm_rotate_at is None

        session_result = await db_session.execute(select(Session).where(Session.id == session.id))
        updated_session = session_result.scalars().first()
        assert updated_session is not None
        assert updated_session.observed_state == SessionStatus.STOPPED
        assert updated_session.endpoint is None

    @pytest.mark.asyncio
    async def test_reconcile_keeps_available_sandbox_when_multi_runtime_alive(
        self,
        db_session,
        driver,
        monkeypatch,
    ):
        sandbox_mgr = SandboxManager(driver=driver, db_session=db_session)
        sandbox = await sandbox_mgr.create_warm_sandbox(profile_id="python-default")
        await sandbox_mgr.mark_warm_available(sandbox.id)

        session = Session(
            id="sess-warm-multi",
            sandbox_id=sandbox.id,
            profile_id="python-default",
            container_id="primary-container",
            endpoint="http://alive-runtime",
            observed_state=SessionStatus.RUNNING,
            desired_state=SessionStatus.RUNNING,
            containers=[
                {
                    "name": "ship",
                    "container_id": "primary-container",
                    "endpoint": "http://alive-runtime",
                    "status": "running",
                    "runtime_type": "ship",
                    "capabilities": ["python"],
                },
                {
                    "name": "browser",
                    "container_id": "browser-container",
                    "endpoint": "http://browser-runtime",
                    "status": "running",
                    "runtime_type": "browser",
                    "capabilities": ["browser"],
                },
            ],
        )
        db_session.add(session)
        sandbox.current_session_id = session.id
        await db_session.commit()

        queue = _FakeWarmupQueue()
        scheduler = WarmPoolScheduler(
            config=SimpleNamespace(interval_seconds=60, run_on_startup=False),
            warmup_queue=queue,
        )

        async def _list_runtime_instances(*, labels):
            assert labels == {"bay.session_id": session.id}
            return [
                RuntimeInstance(
                    id="runtime-1",
                    name="bay-session-sess-warm-multi",
                    labels={"bay.session_id": session.id},
                    state=ContainerStatus.RUNNING.value,
                )
            ]

        driver.list_runtime_instances = _list_runtime_instances

        class _SessionFactory:
            async def __aenter__(self):
                return db_session

            async def __aexit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            "app.db.session.get_async_session",
            lambda: _SessionFactory(),
        )
        monkeypatch.setattr("app.api.dependencies.get_driver", lambda: driver)

        reconciled = await scheduler._reconcile_profile_runtime_state("python-default")
        assert reconciled == 0
        assert queue.enqueued == []

        from sqlmodel import select

        sandbox_result = await db_session.execute(select(Sandbox).where(Sandbox.id == sandbox.id))
        updated_sandbox = sandbox_result.scalars().first()
        assert updated_sandbox is not None
        assert updated_sandbox.warm_state == WarmState.AVAILABLE.value
