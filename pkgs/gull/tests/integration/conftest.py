"""Gull integration test configuration.

These tests require a running Gull container (gull:latest image).
They verify the HTTP API endpoints by starting a container, sending
HTTP requests, and validating responses.

Usage:
    cd pkgs/gull
    ./tests/scripts/run_integration.sh

Skip condition:
    Tests are skipped when the gull:latest Docker image is not available
    or Docker is not accessible.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

# Container settings
GULL_IMAGE = "gull:latest"
GULL_CONTAINER_NAME = "gull-integration-test"
GULL_HOST_PORT = 18090
GULL_BASE_URL = f"http://localhost:{GULL_HOST_PORT}"
DEFAULT_TIMEOUT = 15.0


def _docker_available() -> bool:
    """Check if Docker CLI is available."""
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _image_exists(image: str) -> bool:
    """Check if a Docker image exists locally."""
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


skip_unless_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available",
)

skip_unless_gull_image = pytest.mark.skipif(
    not _image_exists(GULL_IMAGE),
    reason=f"{GULL_IMAGE} image not found (build pkgs/gull/Dockerfile)",
)


@pytest.fixture(scope="module")
def gull_container():
    """Start a Gull container for the test module, tear down after.

    Yields the base URL for the running container.
    """
    # Clean up any leftover container
    subprocess.run(
        ["docker", "rm", "-f", GULL_CONTAINER_NAME],
        capture_output=True,
        timeout=10,
    )

    # Start container
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            GULL_CONTAINER_NAME,
            "-p",
            f"{GULL_HOST_PORT}:8115",
            GULL_IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Failed to start Gull container: {result.stderr}"

    # Wait for container to be ready
    for i in range(20):
        try:
            resp = httpx.get(f"{GULL_BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        # Dump logs for debugging
        logs = subprocess.run(
            ["docker", "logs", GULL_CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pytest.fail(
            f"Gull container did not become healthy in 20s.\n"
            f"Logs:\n{logs.stdout}\n{logs.stderr}"
        )

    yield GULL_BASE_URL

    # Teardown
    subprocess.run(
        ["docker", "stop", GULL_CONTAINER_NAME],
        capture_output=True,
        timeout=15,
    )
