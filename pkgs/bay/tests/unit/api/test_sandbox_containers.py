"""Unit tests for ContainerRuntimeResponse and sandbox container enrichment.

Tests:
- ContainerRuntimeResponse model serialization
- SandboxResponse with containers field
- _query_single_container fallback behavior
- _query_containers_status timeout handling
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.sandboxes import (
    ContainerRuntimeResponse,
    SandboxResponse,
    _query_containers_status,
    _query_single_container,
)
from app.models.session import Session, SessionStatus


# -- Model serialization tests --


class TestContainerRuntimeResponse:
    """Tests for ContainerRuntimeResponse model."""

    def test_minimal_fields(self):
        """All optional fields default to None."""
        resp = ContainerRuntimeResponse(
            name="ship",
            runtime_type="ship",
            status="running",
            capabilities=["python", "shell"],
        )
        assert resp.name == "ship"
        assert resp.runtime_type == "ship"
        assert resp.status == "running"
        assert resp.version is None
        assert resp.healthy is None
        assert resp.capabilities == ["python", "shell"]

    def test_full_fields(self):
        """All fields populated."""
        resp = ContainerRuntimeResponse(
            name="browser",
            runtime_type="gull",
            status="running",
            version="0.1.2",
            capabilities=["browser"],
            healthy=True,
        )
        assert resp.version == "0.1.2"
        assert resp.healthy is True

    def test_serialization(self):
        """Model serializes to dict correctly."""
        resp = ContainerRuntimeResponse(
            name="ship",
            runtime_type="ship",
            status="running",
            version="0.1.2",
            capabilities=["python"],
            healthy=True,
        )
        data = resp.model_dump()
        assert data == {
            "name": "ship",
            "runtime_type": "ship",
            "status": "running",
            "version": "0.1.2",
            "capabilities": ["python"],
            "healthy": True,
        }

    def test_unhealthy_container(self):
        """Container can be marked as unhealthy."""
        resp = ContainerRuntimeResponse(
            name="ship",
            runtime_type="ship",
            status="running",
            capabilities=[],
            healthy=False,
        )
        assert resp.healthy is False


class TestSandboxResponseWithContainers:
    """Tests for SandboxResponse with containers field."""

    def test_containers_none_by_default(self):
        """Containers field is None when not provided (backward compatible)."""
        resp = SandboxResponse(
            id="sb-test",
            status="ready",
            profile="python-default",
            cargo_id="cargo-test",
            capabilities=["python"],
            created_at=datetime.utcnow(),
            expires_at=None,
            idle_expires_at=None,
        )
        assert resp.containers is None

    def test_containers_with_data(self):
        """Containers field populated with list of ContainerRuntimeResponse."""
        containers = [
            ContainerRuntimeResponse(
                name="ship",
                runtime_type="ship",
                status="running",
                version="0.1.2",
                capabilities=["python", "shell", "filesystem"],
                healthy=True,
            ),
            ContainerRuntimeResponse(
                name="browser",
                runtime_type="gull",
                status="running",
                version="0.1.2",
                capabilities=["browser"],
                healthy=True,
            ),
        ]
        resp = SandboxResponse(
            id="sb-test",
            status="ready",
            profile="browser-enabled",
            cargo_id="cargo-test",
            capabilities=["python", "shell", "filesystem", "browser"],
            created_at=datetime.utcnow(),
            expires_at=None,
            idle_expires_at=None,
            containers=containers,
        )
        assert resp.containers is not None
        assert len(resp.containers) == 2
        assert resp.containers[0].name == "ship"
        assert resp.containers[1].name == "browser"

    def test_json_serialization_with_containers(self):
        """SandboxResponse with containers serializes to JSON correctly."""
        now = datetime(2024, 1, 1, 0, 0, 0)
        resp = SandboxResponse(
            id="sb-test",
            status="ready",
            profile="python-default",
            cargo_id="cargo-test",
            capabilities=["python"],
            created_at=now,
            expires_at=now,
            idle_expires_at=now,
            containers=[
                ContainerRuntimeResponse(
                    name="ship",
                    runtime_type="ship",
                    status="running",
                    version="0.1.2",
                    capabilities=["python"],
                    healthy=True,
                ),
            ],
        )
        data = resp.model_dump()
        assert "containers" in data
        assert len(data["containers"]) == 1
        assert data["containers"][0]["version"] == "0.1.2"

    def test_json_serialization_without_containers(self):
        """SandboxResponse without containers excludes it when None."""
        resp = SandboxResponse(
            id="sb-test",
            status="idle",
            profile="python-default",
            cargo_id="cargo-test",
            capabilities=["python"],
            created_at=datetime.utcnow(),
            expires_at=None,
            idle_expires_at=None,
        )
        data = resp.model_dump(exclude_none=True)
        assert "containers" not in data


# -- Container query tests --


@dataclass
class FakeRuntimeMeta:
    """Fake RuntimeMeta for testing."""

    name: str = "ship"
    version: str = "0.1.2"
    api_version: str = "v1"
    mount_path: str = "/workspace"
    capabilities: dict = None

    def __post_init__(self):
        if self.capabilities is None:
            self.capabilities = {"python": {}, "shell": {}}


class TestQuerySingleContainer:
    """Tests for _query_single_container helper."""

    @pytest.mark.asyncio
    async def test_running_container_success(self):
        """Successfully query version and health from a running container."""
        container = {
            "name": "ship",
            "runtime_type": "ship",
            "status": "running",
            "endpoint": "http://localhost:8123",
            "capabilities": ["python", "shell"],
        }

        mock_adapter = AsyncMock()
        mock_adapter.get_meta.return_value = FakeRuntimeMeta(version="0.1.2")
        mock_adapter.health.return_value = True

        with patch(
            "app.api.v1.sandboxes._make_adapter", return_value=mock_adapter
        ):
            result = await _query_single_container(container)

        assert result.name == "ship"
        assert result.version == "0.1.2"
        assert result.healthy is True
        assert result.status == "running"
        assert result.capabilities == ["python", "shell"]

    @pytest.mark.asyncio
    async def test_stopped_container_no_query(self):
        """Stopped container should not be queried."""
        container = {
            "name": "ship",
            "runtime_type": "ship",
            "status": "stopped",
            "endpoint": "http://localhost:8123",
            "capabilities": ["python"],
        }

        result = await _query_single_container(container)
        assert result.version is None
        assert result.healthy is None
        assert result.status == "stopped"

    @pytest.mark.asyncio
    async def test_no_endpoint_no_query(self):
        """Container without endpoint should not be queried."""
        container = {
            "name": "ship",
            "runtime_type": "ship",
            "status": "running",
            "capabilities": ["python"],
        }

        result = await _query_single_container(container)
        assert result.version is None
        assert result.healthy is None

    @pytest.mark.asyncio
    async def test_meta_failure_graceful(self):
        """Meta query failure should degrade gracefully (version=None)."""
        container = {
            "name": "ship",
            "runtime_type": "ship",
            "status": "running",
            "endpoint": "http://localhost:8123",
            "capabilities": ["python"],
        }

        mock_adapter = AsyncMock()
        mock_adapter.get_meta.side_effect = Exception("connection refused")
        mock_adapter.health.return_value = True

        with patch(
            "app.api.v1.sandboxes._make_adapter", return_value=mock_adapter
        ):
            result = await _query_single_container(container)

        assert result.version is None
        assert result.healthy is True

    @pytest.mark.asyncio
    async def test_health_failure_graceful(self):
        """Health check failure should degrade gracefully (healthy=None)."""
        container = {
            "name": "ship",
            "runtime_type": "ship",
            "status": "running",
            "endpoint": "http://localhost:8123",
            "capabilities": ["python"],
        }

        mock_adapter = AsyncMock()
        mock_adapter.get_meta.return_value = FakeRuntimeMeta(version="0.1.2")
        mock_adapter.health.side_effect = Exception("timeout")

        with patch(
            "app.api.v1.sandboxes._make_adapter", return_value=mock_adapter
        ):
            result = await _query_single_container(container)

        assert result.version == "0.1.2"
        assert result.healthy is None

    @pytest.mark.asyncio
    async def test_both_failures_graceful(self):
        """Both meta and health failures should return basic info."""
        container = {
            "name": "ship",
            "runtime_type": "ship",
            "status": "running",
            "endpoint": "http://localhost:8123",
            "capabilities": ["python"],
        }

        mock_adapter = AsyncMock()
        mock_adapter.get_meta.side_effect = Exception("down")
        mock_adapter.health.side_effect = Exception("down")

        with patch(
            "app.api.v1.sandboxes._make_adapter", return_value=mock_adapter
        ):
            result = await _query_single_container(container)

        assert result.name == "ship"
        assert result.version is None
        assert result.healthy is None


class TestQueryContainersStatus:
    """Tests for _query_containers_status helper."""

    def _make_session(
        self,
        *,
        containers: list[dict] | None = None,
        endpoint: str | None = None,
        runtime_type: str = "ship",
        is_ready: bool = True,
    ) -> MagicMock:
        """Create a mock Session for testing."""
        session = MagicMock(spec=Session)
        session.containers = containers
        session.endpoint = endpoint
        session.runtime_type = runtime_type
        session.is_ready = is_ready
        session.sandbox_id = "sb-test"
        return session

    @pytest.mark.asyncio
    async def test_no_containers_no_endpoint(self):
        """Session with no containers and no endpoint returns None."""
        session = self._make_session(containers=None, endpoint=None)
        result = await _query_containers_status(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_single_container_legacy_fallback(self):
        """Legacy single-container session builds from primary fields."""
        session = self._make_session(
            containers=None,
            endpoint="http://localhost:8123",
            runtime_type="ship",
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_meta.return_value = FakeRuntimeMeta(version="0.1.2")
        mock_adapter.health.return_value = True

        with patch(
            "app.api.v1.sandboxes._make_adapter", return_value=mock_adapter
        ):
            result = await _query_containers_status(session)

        assert result is not None
        assert len(result) == 1
        assert result[0].name == "ship"
        assert result[0].version == "0.1.2"
        assert result[0].healthy is True

    @pytest.mark.asyncio
    async def test_multi_container(self):
        """Multi-container session queries all containers."""
        containers = [
            {
                "name": "ship",
                "runtime_type": "ship",
                "status": "running",
                "endpoint": "http://localhost:8123",
                "capabilities": ["python", "shell"],
            },
            {
                "name": "browser",
                "runtime_type": "gull",
                "status": "running",
                "endpoint": "http://localhost:9222",
                "capabilities": ["browser"],
            },
        ]
        session = self._make_session(containers=containers)

        ship_meta = FakeRuntimeMeta(name="ship", version="0.1.2")
        gull_meta = FakeRuntimeMeta(name="gull", version="0.1.2")

        def make_adapter(endpoint, runtime_type):
            adapter = AsyncMock()
            if runtime_type == "ship":
                adapter.get_meta.return_value = ship_meta
            else:
                adapter.get_meta.return_value = gull_meta
            adapter.health.return_value = True
            return adapter

        with patch(
            "app.api.v1.sandboxes._make_adapter", side_effect=make_adapter
        ):
            result = await _query_containers_status(session)

        assert result is not None
        assert len(result) == 2
        assert result[0].name == "ship"
        assert result[0].version == "0.1.2"
        assert result[1].name == "browser"
        assert result[1].version == "0.1.2"

    @pytest.mark.asyncio
    async def test_timeout_returns_basic_info(self):
        """Timeout returns containers with basic info but no version/health."""
        containers = [
            {
                "name": "ship",
                "runtime_type": "ship",
                "status": "running",
                "endpoint": "http://localhost:8123",
                "capabilities": ["python"],
            },
        ]
        session = self._make_session(containers=containers)

        async def slow_query(container):
            await asyncio.sleep(10)
            return ContainerRuntimeResponse(
                name="ship",
                runtime_type="ship",
                status="running",
                capabilities=["python"],
                version="0.1.2",
                healthy=True,
            )

        with patch(
            "app.api.v1.sandboxes._query_single_container",
            side_effect=slow_query,
        ), patch(
            "app.api.v1.sandboxes._CONTAINER_QUERY_TIMEOUT", 0.01
        ):
            result = await _query_containers_status(session)

        assert result is not None
        assert len(result) == 1
        assert result[0].name == "ship"
        # On timeout, version and healthy should be None (basic info only)
        assert result[0].version is None
        assert result[0].healthy is None
