"""Base capability class."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shipyard_neo._http import HTTPClient


class BaseCapability:
    """Base class for sandbox capabilities.

    Each capability (python, shell, filesystem) wraps HTTP calls
    to the corresponding Bay API endpoints.
    """

    def __init__(self, http: HTTPClient, sandbox_id: str) -> None:
        """Initialize capability.

        Args:
            http: HTTP client for making requests
            sandbox_id: ID of the sandbox this capability belongs to
        """
        self._http = http
        self._sandbox_id = sandbox_id

    @property
    def _base_path(self) -> str:
        """Base path for this capability's endpoints."""
        return f"/v1/sandboxes/{self._sandbox_id}"
