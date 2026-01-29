"""Unit tests for authentication.

Tests the authenticate() function and API Key validation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request

from app.api.dependencies import authenticate
from app.config import SecurityConfig, Settings
from app.errors import UnauthorizedError


def create_mock_request(headers: dict[str, str] | None = None) -> Request:
    """Create a mock FastAPI Request with given headers."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = headers or {}
    return mock_request


def create_mock_settings(
    api_key: str | None = None,
    allow_anonymous: bool = True,
) -> Settings:
    """Create mock settings with security configuration."""
    settings = MagicMock(spec=Settings)
    settings.security = SecurityConfig(
        api_key=api_key,
        allow_anonymous=allow_anonymous,
    )
    return settings


class TestAuthenticateAnonymousMode:
    """Test authentication in anonymous mode (allow_anonymous=true)."""

    def test_no_auth_returns_default_owner(self):
        """No Authorization header returns 'default' owner in anonymous mode."""
        request = create_mock_request()
        settings = create_mock_settings(api_key=None, allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_x_owner_header_respected(self):
        """X-Owner header is respected in anonymous mode."""
        request = create_mock_request(headers={"X-Owner": "alice"})
        settings = create_mock_settings(api_key=None, allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "alice"

    def test_valid_api_key_returns_default(self):
        """Valid API key returns 'default' owner."""
        request = create_mock_request(
            headers={"Authorization": "Bearer test-key"}
        )
        settings = create_mock_settings(api_key="test-key", allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_invalid_api_key_raises_401(self):
        """Invalid API key raises UnauthorizedError even in anonymous mode."""
        request = create_mock_request(
            headers={"Authorization": "Bearer wrong-key"}
        )
        settings = create_mock_settings(api_key="test-key", allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Invalid API key" in str(exc_info.value.message)

    def test_any_token_accepted_without_api_key(self):
        """Any Bearer token is accepted when api_key is not configured."""
        request = create_mock_request(
            headers={"Authorization": "Bearer any-random-token"}
        )
        settings = create_mock_settings(api_key=None, allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"


class TestAuthenticateStrictMode:
    """Test authentication in strict mode (allow_anonymous=false)."""

    def test_no_auth_raises_401(self):
        """No Authorization header raises UnauthorizedError in strict mode."""
        request = create_mock_request()
        settings = create_mock_settings(api_key=None, allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Authentication required" in str(exc_info.value.message)

    def test_x_owner_header_ignored(self):
        """X-Owner header is ignored in strict mode."""
        request = create_mock_request(headers={"X-Owner": "alice"})
        settings = create_mock_settings(api_key=None, allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_valid_api_key_returns_default(self):
        """Valid API key returns 'default' owner in strict mode."""
        request = create_mock_request(
            headers={"Authorization": "Bearer secret-key"}
        )
        settings = create_mock_settings(api_key="secret-key", allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_invalid_api_key_raises_401(self):
        """Invalid API key raises UnauthorizedError in strict mode."""
        request = create_mock_request(
            headers={"Authorization": "Bearer wrong-key"}
        )
        settings = create_mock_settings(api_key="secret-key", allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Invalid API key" in str(exc_info.value.message)

    def test_token_without_api_key_configured_raises_401(self):
        """Bearer token raises 401 when api_key not configured and anonymous disabled."""
        request = create_mock_request(
            headers={"Authorization": "Bearer some-token"}
        )
        settings = create_mock_settings(api_key=None, allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Authentication required" in str(exc_info.value.message)


class TestAuthenticateEdgeCases:
    """Test edge cases and malformed inputs."""

    def test_malformed_auth_header_basic(self):
        """Basic auth header is treated as no token."""
        request = create_mock_request(headers={"Authorization": "Basic abc123"})
        settings = create_mock_settings(api_key=None, allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        # Basic auth is ignored, falls through to anonymous mode
        assert result == "default"

    def test_malformed_auth_header_basic_strict(self):
        """Basic auth header raises 401 in strict mode."""
        request = create_mock_request(headers={"Authorization": "Basic abc123"})
        settings = create_mock_settings(api_key="secret", allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_empty_bearer_token(self):
        """Empty Bearer token is treated as no token."""
        request = create_mock_request(headers={"Authorization": "Bearer "})
        settings = create_mock_settings(api_key="secret", allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # Empty token doesn't match "secret", should raise 401
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_bearer_without_space(self):
        """'Bearer' without space is not a valid prefix."""
        request = create_mock_request(headers={"Authorization": "Bearertoken"})
        settings = create_mock_settings(api_key=None, allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # Not a valid Bearer prefix, falls through to anonymous
            result = authenticate(request)

        assert result == "default"

    def test_case_sensitivity_of_bearer(self):
        """'bearer' (lowercase) is not recognized as valid prefix."""
        request = create_mock_request(headers={"Authorization": "bearer token"})
        settings = create_mock_settings(api_key=None, allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # Lowercase 'bearer' not recognized, falls through to anonymous
            result = authenticate(request)

        assert result == "default"

    def test_whitespace_in_token(self):
        """Token with trailing whitespace should not match."""
        request = create_mock_request(
            headers={"Authorization": "Bearer secret "}  # Trailing space
        )
        settings = create_mock_settings(api_key="secret", allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # "secret " != "secret", should raise 401
            with pytest.raises(UnauthorizedError):
                authenticate(request)


class TestSecurityConfig:
    """Test SecurityConfig model."""

    def test_default_values(self):
        """Test default configuration values."""
        config = SecurityConfig()

        assert config.api_key is None
        assert config.allow_anonymous is True
        assert len(config.blocked_hosts) == 4

    def test_custom_values(self):
        """Test custom configuration values."""
        config = SecurityConfig(
            api_key="my-secret",
            allow_anonymous=False,
            blocked_hosts=["10.0.0.0/8"],
        )

        assert config.api_key == "my-secret"
        assert config.allow_anonymous is False
        assert config.blocked_hosts == ["10.0.0.0/8"]
