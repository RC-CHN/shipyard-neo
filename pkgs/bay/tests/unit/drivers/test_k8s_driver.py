"""Unit tests for K8sDriver.

Tests the K8s driver logic with mocked kubernetes-asyncio API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.drivers.base import ContainerStatus, RuntimeInstance
from app.drivers.k8s.k8s import K8sDriver, _parse_memory, _parse_storage_size

if TYPE_CHECKING:
    pass


class TestParseStorageSize:
    """Test storage size parsing helper function."""

    def test_already_k8s_format_gi(self):
        """Should keep Gi format unchanged."""
        assert _parse_storage_size("1Gi") == "1Gi"
        assert _parse_storage_size("10Gi") == "10Gi"
        assert _parse_storage_size("500Mi") == "500Mi"

    def test_convert_lowercase_g(self):
        """Should convert 'g' to 'Gi'."""
        assert _parse_storage_size("1g") == "1Gi"
        assert _parse_storage_size("10G") == "10Gi"

    def test_convert_lowercase_m(self):
        """Should convert 'm' to 'Mi'."""
        assert _parse_storage_size("512m") == "512Mi"
        assert _parse_storage_size("256M") == "256Mi"

    def test_convert_lowercase_k(self):
        """Should convert 'k' to 'Ki'."""
        assert _parse_storage_size("100k") == "100Ki"
        assert _parse_storage_size("100K") == "100Ki"

    def test_no_suffix(self):
        """Should return as-is if no recognized suffix."""
        assert _parse_storage_size("1000000") == "1000000"

    def test_with_whitespace(self):
        """Should handle whitespace."""
        assert _parse_storage_size("  1Gi  ") == "1Gi"
        assert _parse_storage_size(" 2g ") == "2Gi"


class TestParseMemory:
    """Test memory size parsing helper function."""

    def test_convert_lowercase_g(self):
        """Should convert '1g' to '1Gi'."""
        assert _parse_memory("1g") == "1Gi"
        assert _parse_memory("4G") == "4Gi"

    def test_convert_lowercase_m(self):
        """Should convert '512m' to '512Mi'."""
        assert _parse_memory("512m") == "512Mi"
        assert _parse_memory("256M") == "256Mi"

    def test_already_k8s_format(self):
        """Should keep K8s format unchanged."""
        assert _parse_memory("1Gi") == "1Gi"
        assert _parse_memory("512Mi") == "512Mi"


class TestK8sDriverLabelPrefix:
    """Test label prefix generation."""

    @patch("app.drivers.k8s.k8s.get_settings")
    def test_label_generation(self, mock_settings):
        """Should generate labels with correct prefix."""
        # Setup mock settings
        mock_k8s_cfg = MagicMock()
        mock_k8s_cfg.namespace = "test-ns"
        mock_k8s_cfg.kubeconfig = None
        mock_k8s_cfg.storage_class = None
        mock_k8s_cfg.default_storage_size = "1Gi"
        mock_k8s_cfg.image_pull_secrets = []
        mock_k8s_cfg.pod_startup_timeout = 60
        mock_k8s_cfg.label_prefix = "bay"

        mock_driver_cfg = MagicMock()
        mock_driver_cfg.k8s = mock_k8s_cfg

        mock_settings.return_value.driver = mock_driver_cfg

        driver = K8sDriver()

        assert driver._label("session_id") == "bay.session_id"
        assert driver._label("sandbox_id") == "bay.sandbox_id"
        assert driver._label("managed") == "bay.managed"


class TestK8sDriverStatusMapping:
    """Test Pod phase to ContainerStatus mapping."""

    @pytest.fixture
    def driver(self):
        """Create a K8sDriver instance for testing."""
        with patch("app.drivers.k8s.k8s.get_settings") as mock_settings:
            mock_k8s_cfg = MagicMock()
            mock_k8s_cfg.namespace = "bay"
            mock_k8s_cfg.kubeconfig = None
            mock_k8s_cfg.storage_class = None
            mock_k8s_cfg.default_storage_size = "1Gi"
            mock_k8s_cfg.image_pull_secrets = []
            mock_k8s_cfg.pod_startup_timeout = 60
            mock_k8s_cfg.label_prefix = "bay"

            mock_driver_cfg = MagicMock()
            mock_driver_cfg.k8s = mock_k8s_cfg

            mock_settings.return_value.driver = mock_driver_cfg

            return K8sDriver()

    @pytest.mark.asyncio
    async def test_status_running_with_endpoint(self, driver):
        """Should map Running phase to RUNNING with endpoint."""
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_pod.status.pod_ip = "10.0.0.5"
        mock_pod.status.container_statuses = None

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod.return_value = mock_pod

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                info = await driver.status("test-pod", runtime_port=8123)

                assert info.status == ContainerStatus.RUNNING
                assert info.endpoint == "http://10.0.0.5:8123"
                assert info.exit_code is None

    @pytest.mark.asyncio
    async def test_status_pending_is_created(self, driver):
        """Should map Pending phase to CREATED."""
        mock_pod = MagicMock()
        mock_pod.status.phase = "Pending"
        mock_pod.status.pod_ip = None
        mock_pod.status.container_statuses = None

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod.return_value = mock_pod

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                info = await driver.status("test-pod", runtime_port=8123)

                assert info.status == ContainerStatus.CREATED
                assert info.endpoint is None

    @pytest.mark.asyncio
    async def test_status_failed_is_exited(self, driver):
        """Should map Failed phase to EXITED."""
        mock_pod = MagicMock()
        mock_pod.status.phase = "Failed"
        mock_pod.status.pod_ip = None

        # Simulate container with exit code
        mock_container_status = MagicMock()
        mock_container_status.state.terminated.exit_code = 1
        mock_pod.status.container_statuses = [mock_container_status]

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod.return_value = mock_pod

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                info = await driver.status("test-pod", runtime_port=8123)

                assert info.status == ContainerStatus.EXITED
                assert info.exit_code == 1

    @pytest.mark.asyncio
    async def test_status_not_found(self, driver):
        """Should return NOT_FOUND for 404 error."""
        from kubernetes_asyncio.client import ApiException

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_pod.side_effect = ApiException(status=404)

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                info = await driver.status("nonexistent-pod")

                assert info.status == ContainerStatus.NOT_FOUND


class TestK8sDriverListRuntimeInstances:
    """Test runtime instance discovery for GC."""

    @pytest.fixture
    def driver(self):
        """Create a K8sDriver instance for testing."""
        with patch("app.drivers.k8s.k8s.get_settings") as mock_settings:
            mock_k8s_cfg = MagicMock()
            mock_k8s_cfg.namespace = "bay"
            mock_k8s_cfg.kubeconfig = None
            mock_k8s_cfg.storage_class = None
            mock_k8s_cfg.default_storage_size = "1Gi"
            mock_k8s_cfg.image_pull_secrets = []
            mock_k8s_cfg.pod_startup_timeout = 60
            mock_k8s_cfg.label_prefix = "bay"

            mock_driver_cfg = MagicMock()
            mock_driver_cfg.k8s = mock_k8s_cfg

            mock_settings.return_value.driver = mock_driver_cfg

            return K8sDriver()

    @pytest.mark.asyncio
    async def test_list_runtime_instances_returns_pods(self, driver):
        """Should return list of RuntimeInstance from Pods."""
        # Create mock pods
        mock_pod1 = MagicMock()
        mock_pod1.metadata.name = "bay-session-abc123"
        mock_pod1.metadata.labels = {"bay.managed": "true", "bay.session_id": "abc123"}
        mock_pod1.metadata.creation_timestamp = datetime(
            2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc
        )
        mock_pod1.status.phase = "Running"

        mock_pod2 = MagicMock()
        mock_pod2.metadata.name = "bay-session-def456"
        mock_pod2.metadata.labels = {"bay.managed": "true", "bay.session_id": "def456"}
        mock_pod2.metadata.creation_timestamp = datetime(
            2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc
        )
        mock_pod2.status.phase = "Pending"

        mock_pod_list = MagicMock()
        mock_pod_list.items = [mock_pod1, mock_pod2]

        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = mock_pod_list

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                instances = await driver.list_runtime_instances(
                    labels={"bay.managed": "true"}
                )

                assert len(instances) == 2
                assert instances[0].id == "bay-session-abc123"
                assert instances[0].name == "bay-session-abc123"
                assert instances[0].state == "running"
                assert instances[1].state == "pending"

                # Check label selector was built correctly
                mock_v1.list_namespaced_pod.assert_called_once()
                call_args = mock_v1.list_namespaced_pod.call_args
                assert call_args.kwargs["label_selector"] == "bay.managed=true"

    @pytest.mark.asyncio
    async def test_list_runtime_instances_empty(self, driver):
        """Should return empty list when no pods match."""
        mock_pod_list = MagicMock()
        mock_pod_list.items = []

        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = mock_pod_list

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                instances = await driver.list_runtime_instances(
                    labels={"bay.managed": "true"}
                )

                assert len(instances) == 0


class TestK8sDriverVolumeOperations:
    """Test PVC operations."""

    @pytest.fixture
    def driver(self):
        """Create a K8sDriver instance for testing."""
        with patch("app.drivers.k8s.k8s.get_settings") as mock_settings:
            mock_k8s_cfg = MagicMock()
            mock_k8s_cfg.namespace = "bay"
            mock_k8s_cfg.kubeconfig = None
            mock_k8s_cfg.storage_class = "fast-storage"
            mock_k8s_cfg.default_storage_size = "2Gi"
            mock_k8s_cfg.image_pull_secrets = []
            mock_k8s_cfg.pod_startup_timeout = 60
            mock_k8s_cfg.label_prefix = "bay"

            mock_driver_cfg = MagicMock()
            mock_driver_cfg.k8s = mock_k8s_cfg

            mock_settings.return_value.driver = mock_driver_cfg

            return K8sDriver()

    @pytest.mark.asyncio
    async def test_volume_exists_true(self, driver):
        """Should return True when PVC exists."""
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = MagicMock()

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                exists = await driver.volume_exists("test-pvc")

                assert exists is True

    @pytest.mark.asyncio
    async def test_volume_exists_false(self, driver):
        """Should return False when PVC not found."""
        from kubernetes_asyncio.client import ApiException

        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(
            status=404
        )

        with patch.object(driver, "_get_api_client") as mock_client:
            with patch("app.drivers.k8s.k8s.client.CoreV1Api", return_value=mock_v1):
                mock_client.return_value = MagicMock()

                exists = await driver.volume_exists("nonexistent-pvc")

                assert exists is False


class TestK8sDriverBuildLabels:
    """Test label building logic."""

    @pytest.fixture
    def driver(self):
        """Create a K8sDriver instance for testing."""
        with patch("app.drivers.k8s.k8s.get_settings") as mock_settings:
            mock_k8s_cfg = MagicMock()
            mock_k8s_cfg.namespace = "bay"
            mock_k8s_cfg.kubeconfig = None
            mock_k8s_cfg.storage_class = None
            mock_k8s_cfg.default_storage_size = "1Gi"
            mock_k8s_cfg.image_pull_secrets = []
            mock_k8s_cfg.pod_startup_timeout = 60
            mock_k8s_cfg.label_prefix = "bay"

            mock_driver_cfg = MagicMock()
            mock_driver_cfg.k8s = mock_k8s_cfg

            mock_gc_cfg = MagicMock()
            mock_gc_cfg.get_instance_id.return_value = "test-instance"

            mock_settings.return_value.driver = mock_driver_cfg
            mock_settings.return_value.gc = mock_gc_cfg

            return K8sDriver()

    def test_build_labels(self, driver):
        """Should build correct labels for Pod."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.sandbox_id = "sb-456"

        mock_cargo = MagicMock()
        mock_cargo.id = "cargo-789"

        with patch("app.drivers.k8s.k8s.get_settings") as mock_settings:
            mock_gc_cfg = MagicMock()
            mock_gc_cfg.get_instance_id.return_value = "test-instance"
            mock_settings.return_value.gc = mock_gc_cfg

            labels = driver._build_labels(
                session=mock_session,
                cargo=mock_cargo,
                profile_id="python-default",
                runtime_port=8123,
                extra={"custom": "label"},
            )

            assert labels["bay.session_id"] == "sess-123"
            assert labels["bay.sandbox_id"] == "sb-456"
            assert labels["bay.cargo_id"] == "cargo-789"
            assert labels["bay.profile_id"] == "python-default"
            assert labels["bay.runtime_port"] == "8123"
            assert labels["bay.managed"] == "true"
            assert labels["custom"] == "label"
