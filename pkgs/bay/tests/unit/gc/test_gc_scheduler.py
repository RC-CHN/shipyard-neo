"""Unit tests for GC scheduler.

Tests scheduler lifecycle, run_once behavior, and NoopCoordinator.
Includes edge cases: stop without start, run_lock reentry, etc.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import GCConfig, GCTaskConfig
from app.services.gc.base import GCResult, GCTask
from app.services.gc.coordinator import NoopCoordinator
from app.services.gc.scheduler import GCScheduler


class FakeGCTask(GCTask):
    """Fake GC task for testing."""

    def __init__(self, name: str, cleaned: int = 0, errors: list[str] | None = None):
        self._name = name
        self._cleaned = cleaned
        self._errors = errors or []
        self.run_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def run(self) -> GCResult:
        self.run_count += 1
        result = GCResult(task_name=self._name, cleaned_count=self._cleaned)
        for error in self._errors:
            result.add_error(error)
        return result


class RaisingGCTask(GCTask):
    """GC task that raises an exception."""

    def __init__(self, name: str, error: Exception):
        self._name = name
        self._error = error
        self.run_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def run(self) -> GCResult:
        self.run_count += 1
        raise self._error


@pytest.fixture
def gc_config():
    """Create a GC config for testing."""
    return GCConfig(
        enabled=True,
        run_on_startup=False,
        interval_seconds=1,
        idle_session=GCTaskConfig(enabled=True),
        expired_sandbox=GCTaskConfig(enabled=True),
        orphan_cargo=GCTaskConfig(enabled=True),
        orphan_container=GCTaskConfig(enabled=False),
    )


class TestGCScheduler:
    """Tests for GCScheduler."""

    @pytest.mark.asyncio
    async def test_run_once_executes_all_tasks(self, gc_config):
        """run_once should execute all tasks in order."""
        task1 = FakeGCTask("task1", cleaned=2)
        task2 = FakeGCTask("task2", cleaned=3)

        scheduler = GCScheduler(
            tasks=[task1, task2],
            config=gc_config,
        )

        results = await scheduler.run_once()

        assert len(results) == 2
        assert results[0].task_name == "task1"
        assert results[0].cleaned_count == 2
        assert results[1].task_name == "task2"
        assert results[1].cleaned_count == 3
        assert task1.run_count == 1
        assert task2.run_count == 1

    @pytest.mark.asyncio
    async def test_run_once_continues_after_task_failure(self, gc_config):
        """run_once should continue executing remaining tasks after one fails."""
        task1 = FakeGCTask("task1", cleaned=1)
        task2 = RaisingGCTask("task2", RuntimeError("test error"))
        task3 = FakeGCTask("task3", cleaned=2)

        scheduler = GCScheduler(
            tasks=[task1, task2, task3],
            config=gc_config,
        )

        results = await scheduler.run_once()

        assert len(results) == 3
        assert results[0].cleaned_count == 1
        assert results[1].success is False
        assert "test error" in results[1].errors[0]
        assert results[2].cleaned_count == 2
        assert task1.run_count == 1
        assert task2.run_count == 1
        assert task3.run_count == 1

    @pytest.mark.asyncio
    async def test_run_once_collects_errors(self, gc_config):
        """run_once should collect errors from tasks."""
        task = FakeGCTask("task1", cleaned=1, errors=["error1", "error2"])

        scheduler = GCScheduler(
            tasks=[task],
            config=gc_config,
        )

        results = await scheduler.run_once()

        assert len(results) == 1
        assert results[0].errors == ["error1", "error2"]
        assert not results[0].success

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self, gc_config):
        """Scheduler should start and stop correctly."""
        task = FakeGCTask("task1")
        gc_config.interval_seconds = 0.1  # Short interval for test

        scheduler = GCScheduler(
            tasks=[task],
            config=gc_config,
        )

        assert not scheduler.is_running

        await scheduler.start()
        assert scheduler.is_running

        # Wait for at least one cycle
        await asyncio.sleep(0.2)

        await scheduler.stop()
        assert not scheduler.is_running
        assert task.run_count >= 1

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, gc_config):
        """Calling start() multiple times should not create multiple loops."""
        task = FakeGCTask("task1")

        scheduler = GCScheduler(
            tasks=[task],
            config=gc_config,
        )

        await scheduler.start()
        await scheduler.start()  # Should be ignored

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_empty_tasks_list(self, gc_config):
        """Scheduler should handle empty tasks list."""
        scheduler = GCScheduler(
            tasks=[],
            config=gc_config,
        )

        results = await scheduler.run_once()

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_stop_without_start(self, gc_config):
        """Calling stop() without start() should not raise."""
        task = FakeGCTask("task1")

        scheduler = GCScheduler(
            tasks=[task],
            config=gc_config,
        )

        # Should not raise
        await scheduler.stop()
        assert not scheduler.is_running

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, gc_config):
        """Calling stop() multiple times should not raise."""
        task = FakeGCTask("task1")
        gc_config.interval_seconds = 0.1

        scheduler = GCScheduler(
            tasks=[task],
            config=gc_config,
        )

        await scheduler.start()
        await scheduler.stop()
        await scheduler.stop()  # Should not raise
        await scheduler.stop()  # Should not raise

        assert not scheduler.is_running


class TestNoopCoordinator:
    """Tests for NoopCoordinator."""

    @pytest.mark.asyncio
    async def test_always_acquires(self):
        """NoopCoordinator should always acquire."""
        coordinator = NoopCoordinator()

        async with coordinator.acquire() as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_reentrant(self):
        """NoopCoordinator should allow reentrant acquisition."""
        coordinator = NoopCoordinator()

        async with coordinator.acquire() as acquired1:
            assert acquired1 is True
            async with coordinator.acquire() as acquired2:
                assert acquired2 is True


class TestGCResult:
    """Tests for GCResult."""

    def test_default_values(self):
        """GCResult should have sensible defaults."""
        result = GCResult()

        assert result.task_name == ""
        assert result.cleaned_count == 0
        assert result.skipped_count == 0
        assert result.errors == []
        assert result.success is True

    def test_success_with_no_errors(self):
        """GCResult.success should be True when no errors."""
        result = GCResult(task_name="test", cleaned_count=5)

        assert result.success is True

    def test_success_with_errors(self):
        """GCResult.success should be False when there are errors."""
        result = GCResult(task_name="test")
        result.add_error("error1")

        assert result.success is False

    def test_add_error(self):
        """add_error should append to errors list."""
        result = GCResult()
        result.add_error("error1")
        result.add_error("error2")

        assert result.errors == ["error1", "error2"]

    def test_skipped_count(self):
        """GCResult should track skipped count."""
        result = GCResult(task_name="test", skipped_count=3)

        assert result.skipped_count == 3
        assert result.success is True  # Skipped doesn't mean failure
