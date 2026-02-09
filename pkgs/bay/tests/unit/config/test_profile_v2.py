"""Unit tests for Profile Schema V2 (multi-container support).

Tests:
- Legacy single-container profile auto-normalization
- Multi-container profile parsing
- Capability routing (primary_for priority)
- Primary container resolution
- Default profile behavior
"""

from __future__ import annotations

import pytest

from app.config import ContainerSpec, ProfileConfig, ResourceSpec, StartupConfig


class TestLegacyProfileNormalization:
    """Test backward compatibility: old format auto-converts to multi-container."""

    def test_legacy_profile_normalizes_to_single_container(self):
        """Legacy format (image field) should auto-convert to containers array."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            capabilities=["python"],
        )
        containers = config.get_containers()
        assert len(containers) == 1
        assert containers[0].name == "primary"
        assert containers[0].image == "ship:latest"

    def test_legacy_profile_preserves_runtime_type(self):
        """Legacy runtime_type should be preserved in container spec."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            runtime_type="ship",
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.runtime_type == "ship"

    def test_legacy_profile_preserves_runtime_port(self):
        """Legacy runtime_port should be preserved in container spec."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            runtime_port=8123,
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.runtime_port == 8123

    def test_legacy_profile_preserves_resources(self):
        """Legacy resources should be preserved in container spec."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            resources=ResourceSpec(cpus=2.0, memory="4g"),
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.resources.cpus == 2.0
        assert primary.resources.memory == "4g"

    def test_legacy_profile_preserves_capabilities(self):
        """Legacy capabilities should be preserved in container spec."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            capabilities=["filesystem", "python"],
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.capabilities == ["filesystem", "python"]

    def test_legacy_profile_sets_primary_for_from_capabilities(self):
        """Legacy profile should set primary_for = capabilities."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            capabilities=["filesystem", "python"],
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.primary_for == ["filesystem", "python"]

    def test_legacy_profile_preserves_env(self):
        """Legacy env should be preserved in container spec."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            env={"FOO": "bar"},
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.env == {"FOO": "bar"}

    def test_legacy_profile_preserves_idle_timeout(self):
        """idle_timeout is a shared config, not per-container."""
        config = ProfileConfig(
            id="test",
            image="ship:latest",
            idle_timeout=3600,
        )
        assert config.idle_timeout == 3600


class TestDefaultProfileNormalization:
    """Test default profile (no image, no containers specified)."""

    def test_default_profile_creates_primary_container(self):
        """ProfileConfig with only id should create a default primary container."""
        config = ProfileConfig(id="test")
        containers = config.get_containers()
        assert len(containers) == 1

        primary = containers[0]
        assert primary.name == "primary"
        assert primary.image == "ship:latest"
        assert primary.runtime_type == "ship"
        assert primary.runtime_port == 8123
        assert "python" in primary.capabilities


class TestMultiContainerProfile:
    """Test multi-container profile parsing."""

    def test_multi_container_profile_parsing(self):
        """Multi-container profile should parse correctly."""
        config = ProfileConfig(
            id="browser-python",
            containers=[
                ContainerSpec(
                    name="ship",
                    image="ship:latest",
                    capabilities=["python", "shell", "filesystem"],
                    primary_for=["filesystem"],
                ),
                ContainerSpec(
                    name="browser",
                    image="browser-runtime:latest",
                    runtime_type="browser",
                    runtime_port=8080,
                    capabilities=["browser", "screenshot", "filesystem"],
                ),
            ],
        )
        assert len(config.get_containers()) == 2
        assert config.get_containers()[0].name == "ship"
        assert config.get_containers()[1].name == "browser"

    def test_multi_container_clears_legacy_fields(self):
        """Multi-container format should work even with legacy fields set."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(name="ship", image="ship:latest"),
            ],
        )
        # Legacy fields remain None when containers is set
        assert config.containers is not None
        assert len(config.containers) == 1

    def test_multi_container_custom_startup(self):
        """Multi-container profile can define custom startup strategy."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(name="ship", image="ship:latest"),
                ContainerSpec(name="browser", image="browser:latest"),
            ],
            startup=StartupConfig(order="sequential", wait_for_all=True),
        )
        assert config.startup.order == "sequential"
        assert config.startup.wait_for_all is True

    def test_multi_container_default_startup_is_parallel(self):
        """Default startup strategy should be parallel."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(name="ship", image="ship:latest"),
            ],
        )
        assert config.startup.order == "parallel"
        assert config.startup.wait_for_all is True


class TestPrimaryContainerResolution:
    """Test get_primary_container() logic."""

    def test_primary_container_by_name_primary(self):
        """Container named 'primary' should be the primary container."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(name="browser", image="browser:latest"),
                ContainerSpec(name="primary", image="ship:latest"),
            ],
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.name == "primary"

    def test_primary_container_by_name_ship(self):
        """Container named 'ship' should be the primary container."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(name="browser", image="browser:latest"),
                ContainerSpec(name="ship", image="ship:latest"),
            ],
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.name == "ship"

    def test_primary_container_fallback_to_first(self):
        """If no 'primary' or 'ship', first container is primary."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(name="worker-1", image="worker:latest"),
                ContainerSpec(name="worker-2", image="worker:latest"),
            ],
        )
        primary = config.get_primary_container()
        assert primary is not None
        assert primary.name == "worker-1"

    def test_primary_container_empty_returns_none(self):
        """Empty containers list should return None.

        Note: This is a defensive test. In practice, model_post_init
        always ensures containers is populated.
        """
        config = ProfileConfig(id="test")
        # Force containers to empty for testing
        config.containers = []
        assert config.get_primary_container() is None


class TestCapabilityRouting:
    """Test find_container_for_capability() logic."""

    def test_routing_exclusive_capability(self):
        """Capabilities exclusive to one container route correctly."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(
                    name="ship",
                    image="ship:latest",
                    capabilities=["python", "shell", "filesystem"],
                ),
                ContainerSpec(
                    name="browser",
                    image="browser:latest",
                    capabilities=["browser", "screenshot"],
                ),
            ],
        )
        assert config.find_container_for_capability("python").name == "ship"
        assert config.find_container_for_capability("browser").name == "browser"
        assert config.find_container_for_capability("screenshot").name == "browser"

    def test_routing_primary_for_wins_over_capabilities(self):
        """primary_for should take priority over capabilities order."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(
                    name="ship",
                    image="ship:latest",
                    capabilities=["filesystem"],
                    primary_for=["filesystem"],
                ),
                ContainerSpec(
                    name="browser",
                    image="browser:latest",
                    capabilities=["filesystem", "browser"],
                ),
            ],
        )
        # filesystem should route to ship (because primary_for)
        result = config.find_container_for_capability("filesystem")
        assert result is not None
        assert result.name == "ship"

    def test_routing_no_primary_for_first_wins(self):
        """Without primary_for, first container with capability wins."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(
                    name="ship",
                    image="ship:latest",
                    capabilities=["filesystem", "python"],
                ),
                ContainerSpec(
                    name="browser",
                    image="browser:latest",
                    capabilities=["filesystem", "browser"],
                ),
            ],
        )
        # filesystem should route to ship (first in array)
        result = config.find_container_for_capability("filesystem")
        assert result is not None
        assert result.name == "ship"

    def test_routing_unknown_capability_returns_none(self):
        """Unknown capability should return None."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(
                    name="ship",
                    image="ship:latest",
                    capabilities=["python"],
                ),
            ],
        )
        assert config.find_container_for_capability("gpu") is None

    def test_get_all_capabilities(self):
        """get_all_capabilities should merge all containers' capabilities."""
        config = ProfileConfig(
            id="test",
            containers=[
                ContainerSpec(
                    name="ship",
                    image="ship:latest",
                    capabilities=["python", "shell", "filesystem"],
                ),
                ContainerSpec(
                    name="browser",
                    image="browser:latest",
                    capabilities=["browser", "screenshot", "filesystem"],
                ),
            ],
        )
        all_caps = config.get_all_capabilities()
        assert all_caps == {"python", "shell", "filesystem", "browser", "screenshot"}


class TestContainerSpec:
    """Test ContainerSpec model."""

    def test_default_values(self):
        """ContainerSpec should have sensible defaults."""
        spec = ContainerSpec(name="test", image="test:latest")
        assert spec.runtime_type == "ship"
        assert spec.runtime_port == 8123
        assert spec.capabilities == []
        assert spec.primary_for == []
        assert spec.env == {}
        assert spec.health_check_path == "/health"
        assert spec.resources.cpus == 1.0
        assert spec.resources.memory == "1g"

    def test_custom_values(self):
        """ContainerSpec should accept custom values."""
        spec = ContainerSpec(
            name="browser",
            image="browser-runtime:latest",
            runtime_type="browser",
            runtime_port=8080,
            resources=ResourceSpec(cpus=2.0, memory="2g"),
            capabilities=["browser", "screenshot"],
            primary_for=["browser"],
            env={"DISPLAY": ":99"},
            health_check_path="/healthz",
        )
        assert spec.name == "browser"
        assert spec.runtime_type == "browser"
        assert spec.runtime_port == 8080
        assert spec.resources.memory == "2g"
        assert spec.capabilities == ["browser", "screenshot"]
        assert spec.primary_for == ["browser"]
        assert spec.env == {"DISPLAY": ":99"}
        assert spec.health_check_path == "/healthz"


class TestStartupConfig:
    """Test StartupConfig model."""

    def test_default_values(self):
        """StartupConfig defaults to parallel + wait_for_all."""
        config = StartupConfig()
        assert config.order == "parallel"
        assert config.wait_for_all is True

    def test_sequential_order(self):
        """StartupConfig should accept sequential order."""
        config = StartupConfig(order="sequential", wait_for_all=False)
        assert config.order == "sequential"
        assert config.wait_for_all is False


class TestSessionModelMultiContainer:
    """Test Session model Phase 2 extensions."""

    def test_session_containers_default_none(self):
        """Session.containers should default to None."""
        from app.models.session import Session

        session = Session(
            id="sess-test",
            sandbox_id="sbx-test",
        )
        assert session.containers is None

    def test_session_is_multi_container(self):
        """is_multi_container should detect multi-container sessions."""
        from app.models.session import Session

        session = Session(
            id="sess-test",
            sandbox_id="sbx-test",
            containers=[
                {"name": "ship", "container_id": "c1", "endpoint": "http://ship:8123"},
                {"name": "browser", "container_id": "c2", "endpoint": "http://browser:8080"},
            ],
        )
        assert session.is_multi_container is True

    def test_session_not_multi_container_single(self):
        """Single container should not be multi-container."""
        from app.models.session import Session

        session = Session(
            id="sess-test",
            sandbox_id="sbx-test",
            containers=[
                {"name": "ship", "container_id": "c1", "endpoint": "http://ship:8123"},
            ],
        )
        assert session.is_multi_container is False

    def test_session_not_multi_container_none(self):
        """None containers should not be multi-container."""
        from app.models.session import Session

        session = Session(
            id="sess-test",
            sandbox_id="sbx-test",
            containers=None,
        )
        assert session.is_multi_container is False

    def test_get_container_for_capability(self):
        """get_container_for_capability should find correct container."""
        from app.models.session import Session

        session = Session(
            id="sess-test",
            sandbox_id="sbx-test",
            containers=[
                {
                    "name": "ship",
                    "container_id": "c1",
                    "endpoint": "http://ship:8123",
                    "capabilities": ["python", "shell", "filesystem"],
                },
                {
                    "name": "browser",
                    "container_id": "c2",
                    "endpoint": "http://browser:8080",
                    "capabilities": ["browser", "screenshot"],
                },
            ],
        )
        python_container = session.get_container_for_capability("python")
        assert python_container is not None
        assert python_container["name"] == "ship"

        browser_container = session.get_container_for_capability("browser")
        assert browser_container is not None
        assert browser_container["name"] == "browser"

        gpu_container = session.get_container_for_capability("gpu")
        assert gpu_container is None

    def test_get_container_endpoint(self):
        """get_container_endpoint should return endpoint by name."""
        from app.models.session import Session

        session = Session(
            id="sess-test",
            sandbox_id="sbx-test",
            containers=[
                {"name": "ship", "endpoint": "http://ship:8123"},
                {"name": "browser", "endpoint": "http://browser:8080"},
            ],
        )
        assert session.get_container_endpoint("ship") == "http://ship:8123"
        assert session.get_container_endpoint("browser") == "http://browser:8080"
        assert session.get_container_endpoint("unknown") is None

    def test_degraded_status_exists(self):
        """DEGRADED status should exist for multi-container partial failures."""
        from app.models.session import SessionStatus

        assert SessionStatus.DEGRADED == "degraded"
