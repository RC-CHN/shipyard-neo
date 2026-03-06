"""Unit tests for proxy configuration and resolution."""

from __future__ import annotations

from app.config import ProxyConfig, resolve_proxy_env


class TestProxyConfig:
    """Test proxy model behavior."""

    def test_disabled_proxy_returns_empty_env(self):
        cfg = ProxyConfig(enabled=False, http_proxy="http://proxy:7890")
        assert cfg.get_env_vars() == {}

    def test_enabled_proxy_builds_upper_and_lower_case_env(self):
        cfg = ProxyConfig(
            enabled=True,
            http_proxy="http://proxy:7890",
            https_proxy="http://proxy:7890",
        )

        env = cfg.get_env_vars()

        assert env["HTTP_PROXY"] == "http://proxy:7890"
        assert env["http_proxy"] == "http://proxy:7890"
        assert env["HTTPS_PROXY"] == "http://proxy:7890"
        assert env["https_proxy"] == "http://proxy:7890"
        assert "NO_PROXY" in env
        assert "no_proxy" in env

    def test_no_proxy_merges_default_and_custom_entries(self):
        cfg = ProxyConfig(enabled=True, no_proxy="internal.local,api.svc")

        merged = cfg.get_no_proxy()

        assert "localhost" in merged
        assert "10.0.0.0/8" in merged
        assert ".svc.cluster.local" in merged
        assert "internal.local" in merged
        assert "api.svc" in merged


class TestResolveProxyEnv:
    """Test proxy override chain resolution."""

    def test_global_proxy_used_when_no_overrides(self):
        global_proxy = ProxyConfig(enabled=True, http_proxy="http://global:8080")

        env = resolve_proxy_env(
            global_proxy=global_proxy,
            profile_proxy=None,
            container_proxy=None,
        )

        assert env["HTTP_PROXY"] == "http://global:8080"

    def test_profile_proxy_overrides_global(self):
        global_proxy = ProxyConfig(enabled=True, http_proxy="http://global:8080")
        profile_proxy = ProxyConfig(enabled=True, http_proxy="http://profile:8080")

        env = resolve_proxy_env(
            global_proxy=global_proxy,
            profile_proxy=profile_proxy,
            container_proxy=None,
        )

        assert env["HTTP_PROXY"] == "http://profile:8080"

    def test_container_proxy_overrides_profile(self):
        global_proxy = ProxyConfig(enabled=True, http_proxy="http://global:8080")
        profile_proxy = ProxyConfig(enabled=True, http_proxy="http://profile:8080")
        container_proxy = ProxyConfig(enabled=True, http_proxy="http://container:8080")

        env = resolve_proxy_env(
            global_proxy=global_proxy,
            profile_proxy=profile_proxy,
            container_proxy=container_proxy,
        )

        assert env["HTTP_PROXY"] == "http://container:8080"

    def test_profile_can_disable_global_proxy(self):
        global_proxy = ProxyConfig(enabled=True, http_proxy="http://global:8080")
        profile_proxy = ProxyConfig(enabled=False)

        env = resolve_proxy_env(
            global_proxy=global_proxy,
            profile_proxy=profile_proxy,
            container_proxy=None,
        )

        assert env == {}
