"""Tests for SessionManager multi-container support (Phase 2).

Tests the _ensure_running_multi path for multi-container profiles.

Note: We patch [`SessionManager._wait_for_multi_ready()`](pkgs/bay/app/managers/session/session.py:365)
in unit tests because FakeDriver does not run a real HTTP server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ContainerSpec, ProfileConfig
from app.managers.session.session import SessionManager
from app.models.cargo import Cargo
from app.models.session import Session, SessionStatus
from tests.fakes import FakeDriver


def _multi_profile() -> ProfileConfig:
    """Create a browser-python multi-container profile."""
    return ProfileConfig(
        id="browser-python",
        containers=[
            ContainerSpec(
                name="ship",
                image="ship:latest",
                runtime_type="ship",
                runtime_port=8123,
                capabilities=["python", "shell", "filesystem"],
                primary_for=["filesystem"],
            ),
            ContainerSpec(
                name="gull",
                image="gull:latest",
                runtime_type="gull",
                runtime_port=8080,
                capabilities=["browser"],
            ),
        ],
    )


def _single_profile() -> ProfileConfig:
    """Create a single-container profile."""
    return ProfileConfig(
        id="python-default",
        image="ship:latest",
        runtime_type="ship",
        runtime_port=8123,
        capabilities=["python", "shell", "filesystem"],
    )


def _cargo() -> Cargo:
    """Create a test cargo."""
    return Cargo(
        id="cargo-test",
        owner="default",
        managed=True,
        driver_ref="vol-cargo-test",
    )


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def cargo() -> Cargo:
    return _cargo()


@pytest.fixture(autouse=True)
def _patch_wait_for_ready_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real HTTP calls in readiness checks.

    - Multi-container uses [`SessionManager._wait_for_multi_ready()`](pkgs/bay/app/managers/session/session.py:365)
    - Single-container uses [`SessionManager._wait_for_ready()`](pkgs/bay/app/managers/session/session.py:444)

    FakeDriver does not run a real HTTP server, so we patch both to no-op.
    """

    monkeypatch.setattr(
        SessionManager,
        "_wait_for_multi_ready",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        SessionManager,
        "_wait_for_ready",
        AsyncMock(return_value=None),
    )


class TestMultiContainerEnsureRunning:
    """Test SessionManager._ensure_running_multi."""

    @pytest.mark.asyncio
    async def test_multi_container_creates_network_and_containers(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """Multi-container profile creates network + N containers."""
        mgr = SessionManager(driver, db_session)
        profile = _multi_profile()

        # Create session
        db_session.add(cargo)
        await db_session.commit()

        session = await mgr.create("sandbox-1", cargo, profile)
        assert session.observed_state == SessionStatus.PENDING

        # Ensure running -> triggers multi-container path
        session = await mgr.ensure_running(session, cargo, profile)

        # Verify network was created
        assert len(driver.create_network_calls) == 1
        assert driver.create_network_calls[0] == session.id

        # Verify containers were created
        assert len(driver.create_multi_calls) == 1
        assert driver.create_multi_calls[0]["session_id"] == session.id

        # Verify containers were started
        assert len(driver.start_multi_calls) == 1
        assert set(driver.start_multi_calls[0]) == {"ship", "gull"}

        # Verify session state
        assert session.observed_state == SessionStatus.RUNNING
        assert session.container_id is not None  # primary (ship)
        assert session.endpoint is not None
        assert session.containers is not None
        assert len(session.containers) == 2

        # Verify containers JSON
        container_names = {c["name"] for c in session.containers}
        assert container_names == {"ship", "gull"}

        for c in session.containers:
            assert c["container_id"] is not None
            assert c["endpoint"] is not None
            assert c["status"] == "running"

    @pytest.mark.asyncio
    async def test_multi_container_primary_is_ship(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """Primary container should be 'ship' for backward compatibility."""
        from app.managers.session import SessionManager

        mgr = SessionManager(driver, db_session)
        profile = _multi_profile()

        db_session.add(cargo)
        await db_session.commit()

        session = await mgr.create("sandbox-1", cargo, profile)
        session = await mgr.ensure_running(session, cargo, profile)

        # endpoint should point to ship container
        ship_container = next(c for c in session.containers if c["name"] == "ship")
        assert session.endpoint == ship_container["endpoint"]
        assert session.container_id == ship_container["container_id"]

    @pytest.mark.asyncio
    async def test_multi_container_idempotent(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """Calling ensure_running twice should not create again."""
        from app.managers.session import SessionManager

        mgr = SessionManager(driver, db_session)
        profile = _multi_profile()

        db_session.add(cargo)
        await db_session.commit()

        session = await mgr.create("sandbox-1", cargo, profile)
        session = await mgr.ensure_running(session, cargo, profile)

        # Second call should be no-op
        session = await mgr.ensure_running(session, cargo, profile)

        assert len(driver.create_network_calls) == 1
        assert len(driver.create_multi_calls) == 1

    @pytest.mark.asyncio
    async def test_multi_container_create_failure_rollback(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """If container creation fails, all containers and network are rolled back."""
        from app.managers.session import SessionManager

        mgr = SessionManager(driver, db_session)
        profile = _multi_profile()

        db_session.add(cargo)
        await db_session.commit()

        # Make gull container creation fail
        driver.set_create_multi_fail_on("gull")

        session = await mgr.create("sandbox-1", cargo, profile)

        with pytest.raises(RuntimeError, match="Fake: create_multi failed"):
            await mgr.ensure_running(session, cargo, profile)

        # Session should be FAILED
        assert session.observed_state == SessionStatus.FAILED
        assert session.container_id is None
        assert session.endpoint is None
        assert session.containers is None

        # Network should have been removed
        assert len(driver.remove_network_calls) == 1

    @pytest.mark.asyncio
    async def test_single_container_path_unchanged(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """Single-container profile should use Phase 1 path (no network)."""
        from app.managers.session import SessionManager

        mgr = SessionManager(driver, db_session)
        profile = _single_profile()

        db_session.add(cargo)
        await db_session.commit()

        session = await mgr.create("sandbox-1", cargo, profile)
        session = await mgr.ensure_running(session, cargo, profile)

        # No multi-container calls
        assert not hasattr(driver, "_networks") or len(driver._networks) == 0
        assert session.containers is None
        assert session.observed_state == SessionStatus.RUNNING


class TestMultiContainerStopDestroy:
    """Test SessionManager stop/destroy for multi-container sessions."""

    @pytest.mark.asyncio
    async def test_stop_multi_container_session(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """Stop should stop all containers and remove network."""
        from app.managers.session import SessionManager

        mgr = SessionManager(driver, db_session)
        profile = _multi_profile()

        db_session.add(cargo)
        await db_session.commit()

        session = await mgr.create("sandbox-1", cargo, profile)
        session = await mgr.ensure_running(session, cargo, profile)

        assert session.is_multi_container

        await mgr.stop(session)

        assert session.observed_state == SessionStatus.STOPPED
        assert session.endpoint is None
        assert session.containers is None
        assert len(driver.stop_multi_calls) == 1
        assert len(driver.remove_network_calls) == 1

    @pytest.mark.asyncio
    async def test_destroy_multi_container_session(
        self, driver: FakeDriver, db_session: AsyncSession, cargo: Cargo
    ):
        """Destroy should destroy all containers and remove network."""
        from app.managers.session import SessionManager

        mgr = SessionManager(driver, db_session)
        profile = _multi_profile()

        db_session.add(cargo)
        await db_session.commit()

        session = await mgr.create("sandbox-1", cargo, profile)
        session = await mgr.ensure_running(session, cargo, profile)

        await mgr.destroy(session)

        assert len(driver.destroy_multi_calls) == 1
        assert len(driver.remove_network_calls) == 1


class TestSessionContainersField:
    """Test Session.containers JSON field and helper methods."""

    def test_is_multi_container_false_for_none(self):
        """is_multi_container should be False when containers is None."""
        session = Session(
            id="sess-1",
            sandbox_id="sbx-1",
        )
        assert not session.is_multi_container

    def test_is_multi_container_false_for_single(self):
        """is_multi_container should be False when only one container."""
        session = Session(
            id="sess-1",
            sandbox_id="sbx-1",
            containers=[{"name": "ship", "container_id": "c1", "capabilities": ["python"]}],
        )
        assert not session.is_multi_container

    def test_is_multi_container_true_for_multiple(self):
        """is_multi_container should be True when >1 containers."""
        session = Session(
            id="sess-1",
            sandbox_id="sbx-1",
            containers=[
                {"name": "ship", "container_id": "c1", "capabilities": ["python"]},
                {"name": "gull", "container_id": "c2", "capabilities": ["browser"]},
            ],
        )
        assert session.is_multi_container

    def test_get_container_for_capability(self):
        """Should find the right container by capability."""
        session = Session(
            id="sess-1",
            sandbox_id="sbx-1",
            containers=[
                {"name": "ship", "container_id": "c1", "endpoint": "http://ship:8123",
                 "capabilities": ["python", "shell", "filesystem"]},
                {"name": "gull", "container_id": "c2", "endpoint": "http://gull:8080",
                 "capabilities": ["browser"]},
            ],
        )

        python_c = session.get_container_for_capability("python")
        assert python_c is not None
        assert python_c["name"] == "ship"

        browser_c = session.get_container_for_capability("browser")
        assert browser_c is not None
        assert browser_c["name"] == "gull"

        unknown_c = session.get_container_for_capability("gpu")
        assert unknown_c is None

    def test_get_container_endpoint(self):
        """Should find endpoint by container name."""
        session = Session(
            id="sess-1",
            sandbox_id="sbx-1",
            containers=[
                {"name": "ship", "container_id": "c1", "endpoint": "http://ship:8123"},
                {"name": "gull", "container_id": "c2", "endpoint": "http://gull:8080"},
            ],
        )

        assert session.get_container_endpoint("ship") == "http://ship:8123"
        assert session.get_container_endpoint("gull") == "http://gull:8080"
        assert session.get_container_endpoint("unknown") is None
