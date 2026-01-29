"""Runtime adapter base class.

BaseAdapter is an abstraction for communicating with different runtimes.
- ShipAdapter: Ship runtime (Python/Shell execution)
- Future: BrowserAdapter, GPUAdapter, etc.

See: plans/phase-1/capability-adapter-design.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeMeta:
    """Runtime metadata from GET /meta."""

    name: str  # e.g., "ship"
    version: str
    api_version: str
    mount_path: str  # e.g., "/workspace"
    capabilities: dict[str, Any]  # capability -> operations


@dataclass
class ExecutionResult:
    """Result of code/command execution."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    data: dict[str, Any] | None = None


class BaseAdapter(ABC):
    """Abstract runtime adapter interface.
    
    Each runtime image implements one adapter.
    Adapter responsibilities:
    1. HTTP communication
    2. Capability method implementation
    3. Error mapping
    4. Meta caching
    """

    @abstractmethod
    async def get_meta(self) -> RuntimeMeta:
        """Get runtime metadata (should implement caching)."""
        ...

    @abstractmethod
    async def health(self) -> bool:
        """Check runtime health."""
        ...

    @abstractmethod
    def supported_capabilities(self) -> list[str]:
        """List of capabilities this adapter supports at code level."""
        ...

    # -- Python capability --
    async def exec_python(
        self,
        code: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """Execute Python code."""
        raise NotImplementedError("python capability not supported")

    # -- Shell capability --
    async def exec_shell(
        self,
        command: str,
        *,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> ExecutionResult:
        """Execute shell command."""
        raise NotImplementedError("shell capability not supported")

    # -- Filesystem capability --
    async def read_file(self, path: str) -> str:
        """Read file content."""
        raise NotImplementedError("filesystem capability not supported")

    async def write_file(self, path: str, content: str) -> None:
        """Write file content."""
        raise NotImplementedError("filesystem capability not supported")

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        """List directory contents."""
        raise NotImplementedError("filesystem capability not supported")

    async def delete_file(self, path: str) -> None:
        """Delete file or directory."""
        raise NotImplementedError("filesystem capability not supported")

    # -- Upload/Download capability --
    async def upload_file(self, path: str, content: bytes) -> None:
        """Upload binary file."""
        raise NotImplementedError("upload capability not supported")

    async def download_file(self, path: str) -> bytes:
        """Download file as bytes."""
        raise NotImplementedError("download capability not supported")
