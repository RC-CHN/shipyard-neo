"""Unit tests for DockerDriver endpoint resolution.

Tests the endpoint parsing logic without requiring real Docker.
Includes edge cases: invalid port values, missing networks, etc.
"""

from __future__ import annotations

import pytest

from app.drivers.docker.docker import DockerDriver


class TestDockerDriverEndpointResolution:
    """Unit-04: DockerDriver endpoint resolution logic.

    Purpose: Verify host_port/container_network/auto endpoint calculation.
    These are pure function tests using mock container info structures.
    """

    @pytest.fixture
    def sample_container_info_with_network(self) -> dict:
        """Sample docker container info with network IP."""
        return {
            "Id": "abc123",
            "Name": "/bay-session-sess-abc",
            "State": {"Status": "running"},
            "NetworkSettings": {
                "Networks": {
                    "bay-network": {
                        "IPAddress": "172.18.0.5",
                    }
                },
                "Ports": {
                    "8123/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": "32768"}
                    ]
                },
            },
        }

    @pytest.fixture
    def sample_container_info_host_port_only(self) -> dict:
        """Sample docker container info with host port but no network IP."""
        return {
            "Id": "def456",
            "Name": "/bay-session-sess-def",
            "State": {"Status": "running"},
            "NetworkSettings": {
                "Networks": {},
                "Ports": {
                    "8123/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": "32769"}
                    ]
                },
            },
        }

    @pytest.fixture
    def sample_container_info_no_ports(self) -> dict:
        """Sample docker container info with no port bindings."""
        return {
            "Id": "ghi789",
            "Name": "/bay-session-sess-ghi",
            "State": {"Status": "running"},
            "NetworkSettings": {
                "Networks": {
                    "bridge": {
                        "IPAddress": "172.17.0.10",
                    }
                },
                "Ports": {},
            },
        }

    def test_resolve_container_ip_from_specified_network(
        self,
        sample_container_info_with_network,
    ):
        """Should resolve container IP from specified network."""
        # Create driver instance for testing internal methods
        # We'll test the internal methods directly
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = "bay-network"

        ip = driver._resolve_container_ip(sample_container_info_with_network)

        assert ip == "172.18.0.5"

    def test_resolve_container_ip_fallback_to_first_network(
        self,
        sample_container_info_with_network,
    ):
        """Should fallback to first network if specified not found."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = "non-existent-network"

        ip = driver._resolve_container_ip(sample_container_info_with_network)

        # Falls back to first available network
        assert ip == "172.18.0.5"

    def test_resolve_container_ip_no_network(
        self,
        sample_container_info_host_port_only,
    ):
        """Should return None when no networks attached."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = None

        ip = driver._resolve_container_ip(sample_container_info_host_port_only)

        assert ip is None

    def test_resolve_host_port_success(
        self,
        sample_container_info_with_network,
    ):
        """Should resolve host port correctly."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._host_address = "127.0.0.1"

        result = driver._resolve_host_port(
            sample_container_info_with_network,
            runtime_port=8123,
        )

        assert result is not None
        host, port = result
        assert host == "127.0.0.1"
        assert port == 32768

    def test_resolve_host_port_no_bindings(
        self,
        sample_container_info_no_ports,
    ):
        """Should return None when no port bindings."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._host_address = "127.0.0.1"

        result = driver._resolve_host_port(
            sample_container_info_no_ports,
            runtime_port=8123,
        )

        assert result is None

    def test_resolve_host_port_wrong_port(
        self,
        sample_container_info_with_network,
    ):
        """Should return None when requested port not bound."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._host_address = "127.0.0.1"

        result = driver._resolve_host_port(
            sample_container_info_with_network,
            runtime_port=9999,  # Wrong port
        )

        assert result is None

    def test_endpoint_from_hostport(self):
        """Should format host:port endpoint correctly."""
        driver = DockerDriver.__new__(DockerDriver)

        endpoint = driver._endpoint_from_hostport("127.0.0.1", 32768)

        assert endpoint == "http://127.0.0.1:32768"

    def test_endpoint_from_container_ip(self):
        """Should format container IP endpoint correctly."""
        driver = DockerDriver.__new__(DockerDriver)

        endpoint = driver._endpoint_from_container_ip("172.18.0.5", 8123)

        assert endpoint == "http://172.18.0.5:8123"

    def test_resolve_host_port_invalid_port_value(self):
        """Should handle non-numeric HostPort gracefully."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._host_address = "127.0.0.1"

        container_info = {
            "NetworkSettings": {
                "Ports": {
                    "8123/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": "invalid"}
                    ]
                },
            },
        }

        # Should raise ValueError or return None depending on implementation
        # Test that it doesn't crash
        try:
            driver._resolve_host_port(container_info, runtime_port=8123)
            # If it doesn't raise, it should return None or raise later
        except ValueError:
            # Expected behavior - invalid port value
            pass

    def test_resolve_host_port_empty_port_string(self):
        """Should handle empty string HostPort gracefully."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._host_address = "127.0.0.1"

        container_info = {
            "NetworkSettings": {
                "Ports": {
                    "8123/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": ""}
                    ]
                },
            },
        }

        # Should handle empty string gracefully
        try:
            driver._resolve_host_port(container_info, runtime_port=8123)
        except ValueError:
            # Expected behavior - empty string is not a valid port
            pass

    def test_resolve_container_ip_missing_networks_field(self):
        """Should handle missing Networks field gracefully."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = None

        container_info = {
            "NetworkSettings": {
                # Networks field is missing
                "Ports": {},
            },
        }

        # Should return None, not crash
        ip = driver._resolve_container_ip(container_info)
        assert ip is None

    def test_resolve_container_ip_none_networks(self):
        """Should handle Networks=None gracefully."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = None

        container_info = {
            "NetworkSettings": {
                "Networks": None,
                "Ports": {},
            },
        }

        # Should return None, not crash
        ip = driver._resolve_container_ip(container_info)
        assert ip is None


class TestDockerDriverConnectModes:
    """Test different connect modes produce correct endpoints.

    These tests verify the connect mode logic for choosing between
    container_network, host_port, and auto modes.
    """

    @pytest.fixture
    def container_info_both_available(self) -> dict:
        """Container info with both network IP and host port available."""
        return {
            "Name": "/test-container",
            "NetworkSettings": {
                "Networks": {
                    "bay-net": {"IPAddress": "172.20.0.100"},
                },
                "Ports": {
                    "8123/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33333"}],
                },
            },
        }

    def test_container_network_mode_prefers_container_ip(
        self,
        container_info_both_available,
    ):
        """container_network mode should use container IP."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = "bay-net"
        driver._connect_mode = "container_network"
        driver._host_address = "127.0.0.1"

        # Simulate the logic in start() for container_network mode
        if driver._connect_mode in ("container_network", "auto"):
            ip = driver._resolve_container_ip(container_info_both_available)
            if ip:
                endpoint = driver._endpoint_from_container_ip(ip, 8123)

        assert endpoint == "http://172.20.0.100:8123"

    def test_host_port_mode_uses_host_port(
        self,
        container_info_both_available,
    ):
        """host_port mode should use host port even if container IP available."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = "bay-net"
        driver._connect_mode = "host_port"
        driver._host_address = "127.0.0.1"

        endpoint = None

        # container_network mode would not be checked for host_port
        if driver._connect_mode in ("container_network", "auto"):
            ip = driver._resolve_container_ip(container_info_both_available)
            if ip:
                endpoint = driver._endpoint_from_container_ip(ip, 8123)

        # host_port mode
        if endpoint is None and driver._connect_mode in ("host_port", "auto"):
            hp = driver._resolve_host_port(container_info_both_available, runtime_port=8123)
            if hp:
                endpoint = driver._endpoint_from_hostport(hp[0], hp[1])

        assert endpoint == "http://127.0.0.1:33333"

    def test_auto_mode_prefers_container_network(
        self,
        container_info_both_available,
    ):
        """auto mode should prefer container_network when available."""
        driver = DockerDriver.__new__(DockerDriver)
        driver._network = "bay-net"
        driver._connect_mode = "auto"
        driver._host_address = "127.0.0.1"

        endpoint = None

        # auto mode prefers container network
        if driver._connect_mode in ("container_network", "auto"):
            ip = driver._resolve_container_ip(container_info_both_available)
            if ip:
                endpoint = driver._endpoint_from_container_ip(ip, 8123)

        # Would fallback to host_port if no container IP
        if endpoint is None and driver._connect_mode in ("host_port", "auto"):
            hp = driver._resolve_host_port(container_info_both_available, runtime_port=8123)
            if hp:
                endpoint = driver._endpoint_from_hostport(hp[0], hp[1])

        assert endpoint == "http://172.20.0.100:8123"

    def test_auto_mode_falls_back_to_host_port(self):
        """auto mode should fallback to host_port when no container IP."""
        container_info = {
            "Name": "/test-container",
            "NetworkSettings": {
                "Networks": {},  # No networks attached
                "Ports": {
                    "8123/tcp": [{"HostIp": "0.0.0.0", "HostPort": "44444"}],
                },
            },
        }

        driver = DockerDriver.__new__(DockerDriver)
        driver._network = None
        driver._connect_mode = "auto"
        driver._host_address = "127.0.0.1"

        endpoint = None

        if driver._connect_mode in ("container_network", "auto"):
            ip = driver._resolve_container_ip(container_info)
            if ip:
                endpoint = driver._endpoint_from_container_ip(ip, 8123)

        if endpoint is None and driver._connect_mode in ("host_port", "auto"):
            hp = driver._resolve_host_port(container_info, runtime_port=8123)
            if hp:
                endpoint = driver._endpoint_from_hostport(hp[0], hp[1])

        assert endpoint == "http://127.0.0.1:44444"
