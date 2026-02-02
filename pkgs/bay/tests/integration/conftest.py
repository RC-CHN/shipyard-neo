"""E2E API tests configuration and shared fixtures for Bay.

These tests require:
- Docker daemon running and accessible
- ship:latest image built and available
- Bay server running on http://localhost:8000

See: plans/phase-1/tests.md section 1
"""

from __future__ import annotations

import os
import subprocess

import httpx
import pytest

# Bay API base URL - can be overridden by E2E_BAY_PORT environment variable
_bay_port = os.environ.get("E2E_BAY_PORT", "8001")
BAY_BASE_URL = f"http://127.0.0.1:{_bay_port}"

# Test configuration
# API Key for E2E tests (must match config in tests/scripts/docker-host/config.yaml)
E2E_API_KEY = os.environ.get("E2E_API_KEY", "e2e-test-api-key")
AUTH_HEADERS = {"Authorization": f"Bearer {E2E_API_KEY}"}
DEFAULT_PROFILE = "python-default"


def is_bay_running() -> bool:
    """Check if Bay is running."""
    try:
        response = httpx.get(f"{BAY_BASE_URL}/health", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


def is_docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_ship_image_available() -> bool:
    """Check if ship:latest image exists."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "ship:latest"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def docker_volume_exists(volume_name: str) -> bool:
    """Check if a Docker volume exists."""
    try:
        result = subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def docker_container_exists(container_name: str) -> bool:
    """Check if a Docker container exists (running or stopped)."""
    try:
        result = subprocess.run(
            ["docker", "container", "inspect", container_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip all E2E tests if prerequisites not met
e2e_skipif_marks = [
    pytest.mark.skipif(
        not is_docker_available(),
        reason="Docker is not available",
    ),
    pytest.mark.skipif(
        not is_ship_image_available(),
        reason="ship:latest image not found. Run: cd pkgs/ship && make build",
    ),
    pytest.mark.skipif(
        not is_bay_running(),
        reason="Bay is not running. Start with: cd pkgs/bay && uv run python -m app.main",
    ),
]

# Combined pytest mark for E2E tests
pytestmark = e2e_skipif_marks

# pytest-xdist parallel execution configuration
# Most E2E tests can run in parallel because each creates/deletes its own sandbox.
# GC tests need serial execution to avoid interference with other sandboxes.
#
# Usage:
#   Parallel: pytest tests/integration -n auto --dist loadgroup
#   Serial:   pytest tests/integration (default)
#
# Tests marked with @pytest.mark.xdist_group("gc") will run in the same worker,
# effectively serializing them. Use this for GC tests.
gc_serial_mark = pytest.mark.xdist_group("gc")
