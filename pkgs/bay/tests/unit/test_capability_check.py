"""Unit tests for Profile-level capability enforcement.

Tests the require_capability() dependency factory that validates
whether a sandbox's profile supports a requested capability
BEFORE starting any container.

See: plans/phase-1/profile-capability-enforcement.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.dependencies import require_capability
from app.config import ProfileConfig, Settings
from app.errors import CapabilityNotSupportedError
from app.models.sandbox import Sandbox


@pytest.fixture
def mock_sandbox_mgr():
    """Create a mock SandboxManager."""
    mgr = AsyncMock()
    return mgr


@pytest.fixture
def python_only_profile() -> ProfileConfig:
    """Profile that only supports python capability."""
    return ProfileConfig(
        id="python-only",
        image="ship:latest",
        capabilities=["python"],
    )


@pytest.fixture
def full_profile() -> ProfileConfig:
    """Profile that supports all capabilities."""
    return ProfileConfig(
        id="full-access",
        image="ship:latest",
        capabilities=["filesystem", "shell", "python"],
    )


@pytest.fixture
def sandbox_with_python_only(python_only_profile: ProfileConfig) -> Sandbox:
    """Sandbox using python-only profile."""
    return Sandbox(
        id="sandbox-1",
        owner="test-user",
        profile_id=python_only_profile.id,
        name="test-sandbox",
    )


@pytest.fixture
def sandbox_with_full_access(full_profile: ProfileConfig) -> Sandbox:
    """Sandbox using full-access profile."""
    return Sandbox(
        id="sandbox-2",
        owner="test-user",
        profile_id=full_profile.id,
        name="test-sandbox",
    )


class TestRequireCapability:
    """Tests for the require_capability() dependency factory."""

    @pytest.mark.asyncio
    async def test_allowed_capability_returns_sandbox(
        self,
        mock_sandbox_mgr: AsyncMock,
        sandbox_with_full_access: Sandbox,
        full_profile: ProfileConfig,
    ):
        """When profile supports the capability, dependency returns sandbox."""
        mock_sandbox_mgr.get.return_value = sandbox_with_full_access

        # Create mock settings with our test profile
        mock_settings = MagicMock(spec=Settings)
        mock_settings.get_profile.return_value = full_profile

        with patch("app.api.dependencies.get_settings", return_value=mock_settings):
            dependency = require_capability("python")
            result = await dependency(
                sandbox_id="sandbox-2",
                sandbox_mgr=mock_sandbox_mgr,
                owner="test-user",
            )

        assert result is sandbox_with_full_access
        mock_sandbox_mgr.get.assert_awaited_once_with("sandbox-2", "test-user")
        mock_settings.get_profile.assert_called_once_with("full-access")

    @pytest.mark.asyncio
    async def test_disallowed_capability_raises_error(
        self,
        mock_sandbox_mgr: AsyncMock,
        sandbox_with_python_only: Sandbox,
        python_only_profile: ProfileConfig,
    ):
        """When profile doesn't support the capability, dependency raises error."""
        mock_sandbox_mgr.get.return_value = sandbox_with_python_only

        mock_settings = MagicMock(spec=Settings)
        mock_settings.get_profile.return_value = python_only_profile

        with patch("app.api.dependencies.get_settings", return_value=mock_settings):
            dependency = require_capability("shell")

            with pytest.raises(CapabilityNotSupportedError) as exc_info:
                await dependency(
                    sandbox_id="sandbox-1",
                    sandbox_mgr=mock_sandbox_mgr,
                    owner="test-user",
                )

        assert exc_info.value.code == "capability_not_supported"
        assert "shell" in exc_info.value.message
        assert exc_info.value.details["capability"] == "shell"
        assert exc_info.value.details["available"] == ["python"]

    @pytest.mark.asyncio
    async def test_profile_not_found_raises_error(
        self,
        mock_sandbox_mgr: AsyncMock,
        sandbox_with_python_only: Sandbox,
    ):
        """When profile doesn't exist, dependency raises error."""
        mock_sandbox_mgr.get.return_value = sandbox_with_python_only

        mock_settings = MagicMock(spec=Settings)
        mock_settings.get_profile.return_value = None  # Profile not found

        with patch("app.api.dependencies.get_settings", return_value=mock_settings):
            dependency = require_capability("python")

            with pytest.raises(CapabilityNotSupportedError) as exc_info:
                await dependency(
                    sandbox_id="sandbox-1",
                    sandbox_mgr=mock_sandbox_mgr,
                    owner="test-user",
                )

        assert "Profile not found" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_filesystem_capability_allowed(
        self,
        mock_sandbox_mgr: AsyncMock,
        sandbox_with_full_access: Sandbox,
        full_profile: ProfileConfig,
    ):
        """Filesystem capability check works correctly."""
        mock_sandbox_mgr.get.return_value = sandbox_with_full_access

        mock_settings = MagicMock(spec=Settings)
        mock_settings.get_profile.return_value = full_profile

        with patch("app.api.dependencies.get_settings", return_value=mock_settings):
            dependency = require_capability("filesystem")
            result = await dependency(
                sandbox_id="sandbox-2",
                sandbox_mgr=mock_sandbox_mgr,
                owner="test-user",
            )

        assert result is sandbox_with_full_access

    @pytest.mark.asyncio
    async def test_filesystem_capability_denied(
        self,
        mock_sandbox_mgr: AsyncMock,
        sandbox_with_python_only: Sandbox,
        python_only_profile: ProfileConfig,
    ):
        """Filesystem capability is denied for python-only profile."""
        mock_sandbox_mgr.get.return_value = sandbox_with_python_only

        mock_settings = MagicMock(spec=Settings)
        mock_settings.get_profile.return_value = python_only_profile

        with patch("app.api.dependencies.get_settings", return_value=mock_settings):
            dependency = require_capability("filesystem")

            with pytest.raises(CapabilityNotSupportedError) as exc_info:
                await dependency(
                    sandbox_id="sandbox-1",
                    sandbox_mgr=mock_sandbox_mgr,
                    owner="test-user",
                )

        assert exc_info.value.details["capability"] == "filesystem"
        assert "python" in exc_info.value.details["available"]
        assert "filesystem" not in exc_info.value.details["available"]


class TestCapabilityDependencyTypes:
    """Test that the type alias dependencies work correctly."""

    @pytest.mark.asyncio
    async def test_python_capability_dep(
        self,
        mock_sandbox_mgr: AsyncMock,
        sandbox_with_full_access: Sandbox,
        full_profile: ProfileConfig,
    ):
        """PythonCapabilityDep checks for python capability."""
        # Import the actual dependency type
        from app.api.dependencies import PythonCapabilityDep
        
        # Get the actual dependency function from the Annotated type
        from typing import get_args
        _, depends_instance = get_args(PythonCapabilityDep)
        
        mock_sandbox_mgr.get.return_value = sandbox_with_full_access
        mock_settings = MagicMock(spec=Settings)
        mock_settings.get_profile.return_value = full_profile

        with patch("app.api.dependencies.get_settings", return_value=mock_settings):
            # Call the dependency function directly
            result = await depends_instance.dependency(
                sandbox_id="sandbox-2",
                sandbox_mgr=mock_sandbox_mgr,
                owner="test-user",
            )

        assert result is sandbox_with_full_access
