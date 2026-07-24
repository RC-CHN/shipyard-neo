"""Unit tests for deterministic Docker session-network IPAM."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config import DockerConfig, Settings
from app.drivers.docker.docker import DockerDriver


def _driver_with_client(client: AsyncMock) -> DockerDriver:
    driver = DockerDriver.__new__(DockerDriver)
    driver._get_client = AsyncMock(return_value=client)
    driver._log = MagicMock()
    return driver


def _settings(*, pool: str = "10.252.0.0/16", prefix: int = 24) -> Settings:
    return Settings(
        driver={
            "docker": {
                "session_network_pool": pool,
                "session_network_prefix": prefix,
            }
        }
    )


class TestDockerSessionNetworkConfig:
    def test_defaults_use_private_non_lan_pool(self):
        config = DockerConfig()

        assert config.session_network_pool == "10.252.0.0/16"
        assert config.session_network_prefix == 24

    def test_pool_and_prefix_are_configurable(self):
        config = DockerConfig(
            session_network_pool="10.200.0.0/20",
            session_network_prefix=26,
        )

        assert config.session_network_pool == "10.200.0.0/20"
        assert config.session_network_prefix == 26

    @pytest.mark.parametrize(
        ("pool", "prefix"),
        [
            ("not-a-network", 24),
            ("2001:db8::/64", 24),
            ("10.252.0.0/16", 15),
            ("10.252.0.0/16", 33),
        ],
    )
    def test_invalid_pool_or_prefix_is_rejected(self, pool: str, prefix: int):
        with pytest.raises(ValidationError):
            DockerConfig(
                session_network_pool=pool,
                session_network_prefix=prefix,
            )


class TestDockerSessionNetworkIPAM:
    @pytest.mark.asyncio
    async def test_create_emits_ipam_for_first_deterministic_subnet(self):
        client = AsyncMock()
        client.networks.list.return_value = []
        driver = _driver_with_client(client)

        with patch(
            "app.drivers.docker.docker.get_settings",
            return_value=_settings(pool="10.200.0.0/16", prefix=24),
        ):
            result = await driver.create_session_network("session-1")

        assert result == "bay_net_session-1"
        payload = client.networks.create.await_args.args[0]
        assert payload["IPAM"] == {
            "Driver": "default",
            "Config": [{"Subnet": "10.200.0.0/24"}],
        }

    @pytest.mark.asyncio
    async def test_create_skips_subnets_used_by_existing_networks(self):
        client = AsyncMock()
        client.networks.list.return_value = [
            {
                "Name": "existing-1",
                "IPAM": {"Config": [{"Subnet": "10.252.0.0/24"}]},
            },
            {
                "Name": "existing-2",
                "IPAM": {"Config": [{"Subnet": "10.252.1.0/24"}]},
            },
        ]
        driver = _driver_with_client(client)

        with patch(
            "app.drivers.docker.docker.get_settings",
            return_value=_settings(),
        ):
            await driver.create_session_network("session-2")

        payload = client.networks.create.await_args.args[0]
        assert payload["IPAM"]["Config"] == [{"Subnet": "10.252.2.0/24"}]

    @pytest.mark.asyncio
    async def test_exhausted_pool_raises_without_automatic_ipam_fallback(self):
        client = AsyncMock()
        client.networks.list.return_value = [
            {
                "Name": "existing",
                "IPAM": {"Config": [{"Subnet": "10.252.0.0/30"}]},
            }
        ]
        driver = _driver_with_client(client)

        with (
            patch(
                "app.drivers.docker.docker.get_settings",
                return_value=_settings(pool="10.252.0.0/30", prefix=30),
            ),
            pytest.raises(RuntimeError, match="session network pool exhausted"),
        ):
            await driver.create_session_network("session-3")

        client.networks.create.assert_not_awaited()
