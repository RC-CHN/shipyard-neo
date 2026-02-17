"""Unit tests for authentication.

Tests the authenticate() function and API Key validation.
Includes edge cases and priority handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request

from app.api.dependencies import authenticate
from app.config import SecurityConfig, Settings
from app.errors import UnauthorizedError
from app.services.api_key import ApiKeyService


def create_mock_request(
    headers: dict[str, str] | None = None,
    api_key_hashes: dict[str, str] | None = None,
) -> Request:
    """Create a mock FastAPI Request with given headers."""
    mock_request = MagicMock(spec=Request)
    mock_request.headers = headers or {}
    mock_request.app.state.api_key_hashes = api_key_hashes or {}
    return mock_request


def create_mock_settings(
    allow_anonymous: bool = True,
) -> Settings:
    """Create mock settings with security configuration."""
    settings = MagicMock(spec=Settings)
    settings.security = SecurityConfig(
        allow_anonymous=allow_anonymous,
    )
    return settings


def _hash_for(key: str, owner: str = "default") -> dict[str, str]:
    """Helper: create a DB-style hash dict for testing."""
    return {ApiKeyService.hash_key(key): owner}


class TestAuthenticateAnonymousMode:
    """Test authentication in anonymous mode (allow_anonymous=true)."""

    def test_no_auth_returns_default_owner(self):
        """No Authorization header returns 'default' owner in anonymous mode."""
        request = create_mock_request()
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_x_owner_header_respected(self):
        """X-Owner header is respected in anonymous mode."""
        request = create_mock_request(headers={"X-Owner": "alice"})
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "alice"

    def test_valid_db_key_returns_owner(self):
        """Valid DB key returns the associated owner."""
        hashes = _hash_for("test-key", "default")
        request = create_mock_request(
            headers={"Authorization": "Bearer test-key"},
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_invalid_db_key_raises_401(self):
        """Invalid key raises UnauthorizedError even in anonymous mode."""
        hashes = _hash_for("test-key")
        request = create_mock_request(
            headers={"Authorization": "Bearer wrong-key"},
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Invalid API key" in str(exc_info.value.message)

    def test_any_token_accepted_without_db_keys(self):
        """Any Bearer token is accepted when no DB keys loaded and anonymous mode."""
        request = create_mock_request(
            headers={"Authorization": "Bearer any-random-token"},
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_x_owner_and_valid_bearer_priority(self):
        """When X-Owner and valid Bearer are both present, Bearer takes precedence.

        The authenticate function processes Bearer first, so a valid Bearer
        returns the DB owner regardless of X-Owner.
        """
        hashes = _hash_for("test-key", "db-owner")
        request = create_mock_request(
            headers={
                "X-Owner": "alice",
                "Authorization": "Bearer test-key",
            },
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        # Bearer is processed first, returns DB owner
        assert result == "db-owner"

    def test_x_owner_with_no_db_keys(self):
        """When no DB keys and Bearer is present, anonymous mode returns default."""
        request = create_mock_request(
            headers={
                "X-Owner": "alice",
                "Authorization": "Bearer some-token",
            },
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        # No DB keys, anonymous mode, Bearer returns 'default'
        assert result == "default"


class TestAuthenticateStrictMode:
    """Test authentication in strict mode (allow_anonymous=false)."""

    def test_no_auth_raises_401(self):
        """No Authorization header raises UnauthorizedError in strict mode."""
        request = create_mock_request()
        settings = create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Authentication required" in str(exc_info.value.message)

    def test_x_owner_header_ignored(self):
        """X-Owner header is ignored in strict mode."""
        request = create_mock_request(headers={"X-Owner": "alice"})
        settings = create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_valid_db_key_returns_default(self):
        """Valid DB key returns 'default' owner in strict mode."""
        hashes = _hash_for("secret-key")
        request = create_mock_request(
            headers={"Authorization": "Bearer secret-key"},
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_invalid_db_key_raises_401(self):
        """Invalid key raises UnauthorizedError in strict mode."""
        hashes = _hash_for("secret-key")
        request = create_mock_request(
            headers={"Authorization": "Bearer wrong-key"},
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Invalid API key" in str(exc_info.value.message)

    def test_token_without_db_keys_raises_401(self):
        """Bearer token raises 401 when no DB keys and anonymous disabled."""
        request = create_mock_request(
            headers={"Authorization": "Bearer some-token"},
        )
        settings = create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError) as exc_info:
                authenticate(request)

        assert "Authentication required" in str(exc_info.value.message)


class TestAuthenticateEdgeCases:
    """Test edge cases and malformed inputs."""

    def test_malformed_auth_header_basic(self):
        """Basic auth header is treated as no token."""
        request = create_mock_request(headers={"Authorization": "Basic abc123"})
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        # Basic auth is ignored, falls through to anonymous mode
        assert result == "default"

    def test_malformed_auth_header_basic_strict(self):
        """Basic auth header raises 401 in strict mode."""
        request = create_mock_request(headers={"Authorization": "Basic abc123"})
        settings = create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_empty_bearer_token(self):
        """Empty Bearer token doesn't match any DB key."""
        hashes = _hash_for("secret")
        request = create_mock_request(
            headers={"Authorization": "Bearer "},
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # Empty token doesn't match "secret" hash, should raise 401
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_bearer_without_space(self):
        """'Bearer' without space is not a valid prefix."""
        request = create_mock_request(headers={"Authorization": "Bearertoken"})
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # Not a valid Bearer prefix, falls through to anonymous
            result = authenticate(request)

        assert result == "default"

    def test_case_sensitivity_of_bearer(self):
        """'bearer' (lowercase) is not recognized as valid prefix."""
        request = create_mock_request(headers={"Authorization": "bearer token"})
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # Lowercase 'bearer' not recognized, falls through to anonymous
            result = authenticate(request)

        assert result == "default"

    def test_whitespace_in_token(self):
        """Token with trailing whitespace should not match."""
        hashes = _hash_for("secret")
        request = create_mock_request(
            headers={"Authorization": "Bearer secret "},  # Trailing space
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # "secret " != "secret", should raise 401
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_bearer_with_extra_spaces(self):
        """Bearer with extra spaces between 'Bearer' and token.

        The token is extracted after 'Bearer ', so 'Bearer  token' yields ' token'.
        """
        hashes = _hash_for("secret")
        request = create_mock_request(
            headers={"Authorization": "Bearer  secret"},  # Two spaces
            api_key_hashes=hashes,
        )
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # " secret" (with leading space) != "secret"
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_bearer_mixed_case(self):
        """'BEARER' or 'BEaReR' is not recognized."""
        request = create_mock_request(headers={"Authorization": "BEARER token"})
        settings = create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            # BEARER not recognized, falls through to anonymous
            result = authenticate(request)

        assert result == "default"


class TestSecurityConfig:
    """Test SecurityConfig model."""

    def test_default_values(self):
        """Test default configuration values."""
        config = SecurityConfig()

        assert config.allow_anonymous is True
        assert len(config.blocked_hosts) == 4

    def test_custom_values(self):
        """Test custom configuration values."""
        config = SecurityConfig(
            allow_anonymous=False,
            blocked_hosts=["10.0.0.0/8"],
        )

        assert config.allow_anonymous is False
        assert config.blocked_hosts == ["10.0.0.0/8"]
