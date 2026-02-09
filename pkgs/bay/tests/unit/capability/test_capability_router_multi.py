"""Unit tests for CapabilityRouter multi-container routing.

Covers Phase 2 behavior:
- [`CapabilityRouter._get_adapter()`](pkgs/bay/app/router/capability/capability.py:57) can route by capability
  to the correct container endpoint/runtime_type using [`Session.containers`](pkgs/bay/app/models/session.py:55).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.errors import CapabilityNotSupportedError
from app.models.session import Session
from app.router.capability import CapabilityRouter


@pytest.fixture
def mock_sandbox_mgr() -> AsyncMock:
    return AsyncMock()


def test_get_adapter_routes_to_container_by_capability(mock_sandbox_mgr):
    """Multi-container session should route adapter to matching container."""
    router = CapabilityRouter(mock_sandbox_mgr)

    session = Session(
        id="sess-1",
        sandbox_id="sbx-1",
        runtime_type="ship",  # legacy primary runtime_type
        endpoint="http://primary-ship:8123",
        containers=[
            {
                "name": "ship",
                "container_id": "c-ship",
                "endpoint": "http://ship:8123",
                "status": "running",
                "runtime_type": "ship",
                "capabilities": ["python", "shell", "filesystem"],
            },
            {
                "name": "gull",
                "container_id": "c-gull",
                "endpoint": "http://gull:8080",
                "status": "running",
                "runtime_type": "gull",
                "capabilities": ["browser"],
            },
        ],
    )

    adapter = router._get_adapter(session, capability="browser")

    # GullAdapter type name is enough here (avoid importing adapter class)
    assert adapter.__class__.__name__ == "GullAdapter"


def test_get_adapter_falls_back_to_primary_when_no_capability(mock_sandbox_mgr):
    """If capability is not specified, _get_adapter should use primary endpoint."""
    router = CapabilityRouter(mock_sandbox_mgr)

    session = Session(
        id="sess-1",
        sandbox_id="sbx-1",
        runtime_type="ship",
        endpoint="http://primary-ship:8123",
        containers=[
            {
                "name": "ship",
                "container_id": "c-ship",
                "endpoint": "http://ship:8123",
                "status": "running",
                "runtime_type": "ship",
                "capabilities": ["python"],
            },
            {
                "name": "gull",
                "container_id": "c-gull",
                "endpoint": "http://gull:8080",
                "status": "running",
                "runtime_type": "gull",
                "capabilities": ["browser"],
            },
        ],
    )

    adapter = router._get_adapter(session)
    assert adapter.__class__.__name__ == "ShipAdapter"


def test_get_adapter_missing_capability_raises_with_available(mock_sandbox_mgr):
    """If no container provides the capability, raise with merged available list."""
    router = CapabilityRouter(mock_sandbox_mgr)

    session = Session(
        id="sess-1",
        sandbox_id="sbx-1",
        runtime_type="ship",
        endpoint="http://primary-ship:8123",
        containers=[
            {
                "name": "ship",
                "container_id": "c-ship",
                "endpoint": "http://ship:8123",
                "status": "running",
                "runtime_type": "ship",
                "capabilities": ["python", "filesystem"],
            },
            {
                "name": "gull",
                "container_id": "c-gull",
                "endpoint": "http://gull:8080",
                "status": "running",
                "runtime_type": "gull",
                "capabilities": ["browser"],
            },
        ],
    )

    with pytest.raises(CapabilityNotSupportedError) as exc:
        router._get_adapter(session, capability="gpu")

    err = exc.value
    assert err.details.get("capability") == "gpu"
    # merged and sorted
    assert err.details.get("available") == ["browser", "filesystem", "python"]
