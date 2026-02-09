"""Sandbox resource class."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from shipyard_neo.capabilities.filesystem import FilesystemCapability
from shipyard_neo.capabilities.python import PythonCapability
from shipyard_neo.capabilities.shell import ShellCapability
from shipyard_neo.types import (
    ExecutionHistoryEntry,
    ExecutionHistoryList,
    SandboxInfo,
    SandboxStatus,
)

if TYPE_CHECKING:
    from shipyard_neo._http import HTTPClient


class Sandbox:
    """Sandbox resource - the core abstraction.

    A Sandbox represents an isolated execution environment with:
    - Python execution (via IPython kernel)
    - Shell command execution
    - Filesystem operations

    The underlying Session is managed transparently by Bay.
    Capabilities will auto-start a session if needed.
    """

    def __init__(self, http: HTTPClient, info: SandboxInfo) -> None:
        """Initialize Sandbox.

        Args:
            http: HTTP client for making requests
            info: Sandbox information from API
        """
        self._http = http
        self._info = info

        # Initialize capability objects
        self.python = PythonCapability(http, info.id)
        self.shell = ShellCapability(http, info.id)
        self.filesystem = FilesystemCapability(http, info.id)

    # Properties from SandboxInfo

    @property
    def id(self) -> str:
        """Sandbox ID."""
        return self._info.id

    @property
    def status(self) -> SandboxStatus:
        """Current status (may be stale; call refresh() for latest)."""
        return self._info.status

    @property
    def profile(self) -> str:
        """Profile ID (e.g., 'python-default')."""
        return self._info.profile

    @property
    def cargo_id(self) -> str:
        """Associated cargo ID."""
        return self._info.cargo_id

    @property
    def capabilities(self) -> list[str]:
        """Available capabilities (e.g., ['python', 'shell', 'filesystem'])."""
        return self._info.capabilities

    @property
    def created_at(self) -> datetime:
        """Creation timestamp."""
        return self._info.created_at

    @property
    def expires_at(self) -> datetime | None:
        """TTL expiration timestamp (None = no expiry)."""
        return self._info.expires_at

    @property
    def idle_expires_at(self) -> datetime | None:
        """Idle timeout expiration (None = no session or no idle timeout)."""
        return self._info.idle_expires_at

    # Lifecycle methods

    async def refresh(self) -> None:
        """Refresh sandbox info from API.

        Updates local state with latest status, expires_at, etc.
        """
        response = await self._http.get(f"/v1/sandboxes/{self.id}")
        self._info = SandboxInfo.model_validate(response)

    async def stop(self) -> None:
        """Stop the sandbox session.

        Reclaims compute resources but preserves workspace data.
        After stop, the next capability call will auto-start a new session.
        Python variables will be lost, but files remain.
        """
        await self._http.post(f"/v1/sandboxes/{self.id}/stop")

    async def delete(self) -> None:
        """Delete the sandbox permanently.

        Destroys all running sessions and managed cargo.
        External cargo is NOT deleted.
        """
        await self._http.delete(f"/v1/sandboxes/{self.id}")

    async def extend_ttl(
        self,
        seconds: int,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """Extend sandbox TTL by N seconds.

        Args:
            seconds: Number of seconds to extend by (>= 1)
            idempotency_key: Optional key for safe retries

        Raises:
            SandboxExpiredError: If sandbox has already expired
            SandboxTTLInfiniteError: If sandbox has infinite TTL
        """
        response = await self._http.post(
            f"/v1/sandboxes/{self.id}/extend_ttl",
            json={"extend_by": seconds},
            idempotency_key=idempotency_key,
        )
        self._info = SandboxInfo.model_validate(response)

    async def keepalive(self) -> None:
        """Send keepalive signal.

        Extends idle timeout only, NOT TTL.
        Does NOT start a session if none exists.
        """
        await self._http.post(f"/v1/sandboxes/{self.id}/keepalive")

    # Execution history methods

    async def get_execution_history(
        self,
        *,
        exec_type: str | None = None,
        success_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        tags: str | None = None,
        has_notes: bool = False,
        has_description: bool = False,
    ) -> ExecutionHistoryList:
        """Get execution history for this sandbox."""
        response = await self._http.get(
            f"/v1/sandboxes/{self.id}/history",
            params={
                "exec_type": exec_type,
                "success_only": success_only,
                "limit": limit,
                "offset": offset,
                "tags": tags,
                "has_notes": has_notes,
                "has_description": has_description,
            },
        )
        return ExecutionHistoryList.model_validate(response)

    async def get_execution(self, execution_id: str) -> ExecutionHistoryEntry:
        """Get one execution history record by ID."""
        response = await self._http.get(
            f"/v1/sandboxes/{self.id}/history/{execution_id}"
        )
        return ExecutionHistoryEntry.model_validate(response)

    async def get_last_execution(self, *, exec_type: str | None = None) -> ExecutionHistoryEntry:
        """Get the latest execution history record."""
        response = await self._http.get(
            f"/v1/sandboxes/{self.id}/history/last",
            params={"exec_type": exec_type},
        )
        return ExecutionHistoryEntry.model_validate(response)

    async def annotate_execution(
        self,
        execution_id: str,
        *,
        description: str | None = None,
        tags: str | None = None,
        notes: str | None = None,
    ) -> ExecutionHistoryEntry:
        """Annotate one execution history record."""
        response = await self._http.request(
            "PATCH",
            f"/v1/sandboxes/{self.id}/history/{execution_id}",
            json={
                "description": description,
                "tags": tags,
                "notes": notes,
            },
        )
        return ExecutionHistoryEntry.model_validate(response)
