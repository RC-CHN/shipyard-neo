"""Unit tests for GC tasks.

Tests each GC task's query logic and processing behavior.
Uses mock DB sessions to avoid requiring actual database.
Includes edge cases: _process_sandbox returns False, delete errors, etc.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import GCConfig, GCTaskConfig
from app.drivers.base import RuntimeInstance


class TestIdleSessionGCQueryConditions:
    """Tests for IdleSessionGC query conditions."""

    @pytest.mark.asyncio
    async def test_finds_sandboxes_with_expired_idle_timeout(self):
        """Should find sandboxes where idle_expires_at < now."""
        from app.services.gc.tasks.idle_session import IdleSessionGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        # Mock sandbox that is idle-expired
        expired_sandbox = MagicMock()
        expired_sandbox.id = "sandbox-1"
        expired_sandbox.deleted_at = None
        expired_sandbox.idle_expires_at = datetime.utcnow() - timedelta(minutes=5)

        # Mock the DB query result
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [expired_sandbox]
        db_session.execute.return_value = result_mock

        task = IdleSessionGC(driver, db_session)

        # Mock _process_sandbox to avoid actual processing
        task._process_sandbox = AsyncMock(return_value=True)

        result = await task.run()

        assert result.cleaned_count == 1
        task._process_sandbox.assert_called_once_with("sandbox-1")

    @pytest.mark.asyncio
    async def test_skips_sandboxes_with_null_idle_expires_at(self):
        """Should not process sandboxes without idle_expires_at."""
        from app.services.gc.tasks.idle_session import IdleSessionGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        # No sandboxes match
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        db_session.execute.return_value = result_mock

        task = IdleSessionGC(driver, db_session)
        result = await task.run()

        assert result.cleaned_count == 0
        assert result.skipped_count == 0

    @pytest.mark.asyncio
    async def test_handles_process_errors(self):
        """Should collect errors but continue processing."""
        from app.services.gc.tasks.idle_session import IdleSessionGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        sandbox1 = MagicMock(id="sandbox-1", deleted_at=None)
        sandbox2 = MagicMock(id="sandbox-2", deleted_at=None)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [sandbox1, sandbox2]
        db_session.execute.return_value = result_mock

        task = IdleSessionGC(driver, db_session)
        task._process_sandbox = AsyncMock(side_effect=[RuntimeError("fail1"), True])

        result = await task.run()

        assert result.cleaned_count == 1
        assert len(result.errors) == 1
        assert "sandbox-1" in result.errors[0]

    @pytest.mark.asyncio
    async def test_idle_session_process_returns_false_counts_skipped(self):
        """When _process_sandbox returns False, should count as skipped."""
        from app.services.gc.tasks.idle_session import IdleSessionGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        sandbox1 = MagicMock(id="sandbox-1", deleted_at=None)
        sandbox2 = MagicMock(id="sandbox-2", deleted_at=None)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [sandbox1, sandbox2]
        db_session.execute.return_value = result_mock

        task = IdleSessionGC(driver, db_session)
        # First returns False (skipped), second returns True (cleaned)
        task._process_sandbox = AsyncMock(side_effect=[False, True])

        result = await task.run()

        assert result.cleaned_count == 1
        assert result.skipped_count == 1


class TestExpiredSandboxGCQueryConditions:
    """Tests for ExpiredSandboxGC query conditions."""

    @pytest.mark.asyncio
    async def test_finds_sandboxes_with_expired_ttl(self):
        """Should find sandboxes where expires_at < now."""
        from app.services.gc.tasks.expired_sandbox import ExpiredSandboxGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        expired_sandbox = MagicMock()
        expired_sandbox.id = "sandbox-1"
        expired_sandbox.owner = "default"
        expired_sandbox.cargo_id = "ws-1"
        expired_sandbox.deleted_at = None
        expired_sandbox.expires_at = datetime.utcnow() - timedelta(hours=1)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [expired_sandbox]
        db_session.execute.return_value = result_mock

        task = ExpiredSandboxGC(driver, db_session)
        task._process_sandbox = AsyncMock(return_value=True)

        result = await task.run()

        assert result.cleaned_count == 1

    @pytest.mark.asyncio
    async def test_skips_sandboxes_without_expiry(self):
        """Should not process sandboxes with expires_at = None (infinite TTL)."""
        from app.services.gc.tasks.expired_sandbox import ExpiredSandboxGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        db_session.execute.return_value = result_mock

        task = ExpiredSandboxGC(driver, db_session)
        result = await task.run()

        assert result.cleaned_count == 0

    @pytest.mark.asyncio
    async def test_expired_sandbox_delete_error_collects(self):
        """Should collect delete errors but continue processing."""
        from app.services.gc.tasks.expired_sandbox import ExpiredSandboxGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        sandbox1 = MagicMock(id="sandbox-1", deleted_at=None)
        sandbox2 = MagicMock(id="sandbox-2", deleted_at=None)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [sandbox1, sandbox2]
        db_session.execute.return_value = result_mock

        task = ExpiredSandboxGC(driver, db_session)
        task._process_sandbox = AsyncMock(
            side_effect=[RuntimeError("delete failed"), True]
        )

        result = await task.run()

        # First failed, second succeeded
        assert result.cleaned_count == 1
        assert len(result.errors) == 1
        assert "sandbox-1" in result.errors[0]


class TestOrphanContainerGCStrictMode:
    """Tests for OrphanContainerGC strict mode safety checks."""

    def _create_gc_config(self, instance_id: str = "bay") -> GCConfig:
        config = GCConfig(
            enabled=True,
            instance_id=instance_id,
            orphan_container=GCTaskConfig(enabled=True),
        )
        return config

    @pytest.mark.asyncio
    async def test_skips_containers_without_bay_prefix(self):
        """Should skip containers not named bay-session-*."""
        from app.services.gc.tasks.orphan_container import OrphanContainerGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()
        config = self._create_gc_config()

        # Container with wrong name prefix
        instance = RuntimeInstance(
            id="container-1",
            name="other-container-abc",  # Wrong prefix
            labels={
                "bay.session_id": "sess-1",
                "bay.sandbox_id": "sandbox-1",
                "bay.cargo_id": "ws-1",
                "bay.instance_id": "bay",
                "bay.managed": "true",
            },
            state="running",
        )

        driver.list_runtime_instances = AsyncMock(return_value=[instance])

        task = OrphanContainerGC(driver, db_session, config)
        result = await task.run()

        assert result.cleaned_count == 0
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_skips_containers_with_missing_labels(self):
        """Should skip containers missing required labels."""
        from app.services.gc.tasks.orphan_container import OrphanContainerGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()
        config = self._create_gc_config()

        # Container missing bay.sandbox_id label
        instance = RuntimeInstance(
            id="container-1",
            name="bay-session-sess-1",
            labels={
                "bay.session_id": "sess-1",
                # missing: "bay.sandbox_id"
                "bay.cargo_id": "ws-1",
                "bay.instance_id": "bay",
                "bay.managed": "true",
            },
            state="running",
        )

        driver.list_runtime_instances = AsyncMock(return_value=[instance])

        task = OrphanContainerGC(driver, db_session, config)
        result = await task.run()

        assert result.cleaned_count == 0
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_skips_containers_with_wrong_instance_id(self):
        """Should skip containers with different instance_id."""
        from app.services.gc.tasks.orphan_container import OrphanContainerGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()
        config = self._create_gc_config(instance_id="bay-1")

        # Container belongs to different instance
        instance = RuntimeInstance(
            id="container-1",
            name="bay-session-sess-1",
            labels={
                "bay.session_id": "sess-1",
                "bay.sandbox_id": "sandbox-1",
                "bay.cargo_id": "ws-1",
                "bay.instance_id": "bay-2",  # Different instance
                "bay.managed": "true",
            },
            state="running",
        )

        driver.list_runtime_instances = AsyncMock(return_value=[instance])

        task = OrphanContainerGC(driver, db_session, config)
        result = await task.run()

        assert result.cleaned_count == 0
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_skips_containers_not_managed(self):
        """Should skip containers with bay.managed != 'true'."""
        from app.services.gc.tasks.orphan_container import OrphanContainerGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()
        config = self._create_gc_config()

        instance = RuntimeInstance(
            id="container-1",
            name="bay-session-sess-1",
            labels={
                "bay.session_id": "sess-1",
                "bay.sandbox_id": "sandbox-1",
                "bay.cargo_id": "ws-1",
                "bay.instance_id": "bay",
                "bay.managed": "false",  # Not managed
            },
            state="running",
        )

        driver.list_runtime_instances = AsyncMock(return_value=[instance])

        task = OrphanContainerGC(driver, db_session, config)
        result = await task.run()

        assert result.cleaned_count == 0
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_skips_containers_with_existing_session(self):
        """Should skip containers whose session still exists in DB."""
        from app.services.gc.tasks.orphan_container import OrphanContainerGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()
        config = self._create_gc_config()

        instance = RuntimeInstance(
            id="container-1",
            name="bay-session-sess-1",
            labels={
                "bay.session_id": "sess-1",
                "bay.sandbox_id": "sandbox-1",
                "bay.cargo_id": "ws-1",
                "bay.instance_id": "bay",
                "bay.managed": "true",
            },
            state="running",
        )

        driver.list_runtime_instances = AsyncMock(return_value=[instance])

        # Session exists in DB
        db_result = MagicMock()
        db_result.scalars.return_value.first.return_value = "sess-1"
        db_session.execute.return_value = db_result

        task = OrphanContainerGC(driver, db_session, config)
        result = await task.run()

        assert result.cleaned_count == 0
        assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_deletes_orphan_container(self):
        """Should delete containers whose session doesn't exist in DB."""
        from app.services.gc.tasks.orphan_container import OrphanContainerGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()
        config = self._create_gc_config()

        instance = RuntimeInstance(
            id="container-1",
            name="bay-session-sess-orphan",
            labels={
                "bay.session_id": "sess-orphan",
                "bay.sandbox_id": "sandbox-1",
                "bay.cargo_id": "ws-1",
                "bay.instance_id": "bay",
                "bay.managed": "true",
            },
            state="running",
        )

        driver.list_runtime_instances = AsyncMock(return_value=[instance])
        driver.destroy_runtime_instance = AsyncMock()

        # Session does NOT exist in DB
        db_result = MagicMock()
        db_result.scalars.return_value.first.return_value = None
        db_session.execute.return_value = db_result

        task = OrphanContainerGC(driver, db_session, config)
        result = await task.run()

        assert result.cleaned_count == 1
        driver.destroy_runtime_instance.assert_called_once_with("container-1")


class TestOrphanCargoGC:
    """Tests for OrphanCargoGC."""

    @pytest.mark.asyncio
    async def test_finds_orphan_cargos(self):
        """Should find managed cargos without a valid sandbox."""
        from app.services.gc.tasks.orphan_cargo import OrphanCargoGC
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        # Mock cargo manager
        task = OrphanCargoGC(driver, db_session)
        task._find_orphans = AsyncMock(return_value=["ws-orphan-1", "ws-orphan-2"])
        task._cargo_mgr = MagicMock()
        task._cargo_mgr.delete_internal_by_id = AsyncMock()

        result = await task.run()

        assert result.cleaned_count == 2
        assert task._cargo_mgr.delete_internal_by_id.call_count == 2

    @pytest.mark.asyncio
    async def test_orphan_cargo_no_orphans(self):
        """Should handle case when no orphan cargos are found."""
        from tests.fakes import FakeDriver

        driver = FakeDriver()
        db_session = AsyncMock()

        from app.services.gc.tasks.orphan_cargo import OrphanCargoGC

        task = OrphanCargoGC(driver, db_session)
        task._find_orphans = AsyncMock(return_value=[])
        task._cargo_mgr = MagicMock()
        task._cargo_mgr.delete_internal_by_id = AsyncMock()

        result = await task.run()

        assert result.cleaned_count == 0
        assert task._cargo_mgr.delete_internal_by_id.call_count == 0


class TestGCConfigInstanceId:
    """Tests for GC instance_id resolution."""

    def test_explicit_instance_id(self):
        """Should use explicit instance_id when provided."""
        config = GCConfig(instance_id="my-instance")
        assert config.get_instance_id() == "my-instance"

    def test_fallback_to_hostname(self, monkeypatch):
        """Should fallback to HOSTNAME env var."""
        monkeypatch.setenv("HOSTNAME", "test-hostname")
        config = GCConfig(instance_id=None)
        assert config.get_instance_id() == "test-hostname"

    def test_fallback_to_default(self, monkeypatch):
        """Should fallback to 'bay' when no HOSTNAME."""
        monkeypatch.delenv("HOSTNAME", raising=False)
        config = GCConfig(instance_id=None)
        assert config.get_instance_id() == "bay"


class TestGCTaskConfig:
    """Tests for GC task configuration."""

    def test_orphan_container_disabled_by_default(self):
        """OrphanContainerGC should be disabled by default."""
        config = GCConfig()
        assert config.orphan_container.enabled is False

    def test_other_tasks_enabled_by_default(self):
        """Other GC tasks should be enabled by default."""
        config = GCConfig()
        assert config.idle_session.enabled is True
        assert config.expired_sandbox.enabled is True
        assert config.orphan_cargo.enabled is True

    def test_gc_enabled_by_default(self):
        """GC should be enabled by default."""
        config = GCConfig()
        assert config.enabled is True
        assert config.run_on_startup is True
        assert config.interval_seconds == 300
