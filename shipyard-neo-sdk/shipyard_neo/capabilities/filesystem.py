"""Filesystem capability."""

from __future__ import annotations

from typing import Any

from shipyard_neo.capabilities.base import BaseCapability
from shipyard_neo.types import FileInfo


class FilesystemCapability(BaseCapability):
    """Filesystem operations capability.

    Read, write, list, delete files in the sandbox workspace.
    All paths are relative to /workspace.
    """

    async def read_file(self, path: str) -> str:
        """Read a text file from the sandbox.

        Args:
            path: File path relative to /workspace

        Returns:
            File content as string

        Raises:
            CargoFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
        """
        response = await self._http.get(
            f"{self._base_path}/filesystem/files",
            params={"path": path},
        )
        return response.get("content", "")

    async def write_file(self, path: str, content: str) -> None:
        """Write a text file to the sandbox.

        Creates parent directories if needed.

        Args:
            path: File path relative to /workspace
            content: File content as string

        Raises:
            InvalidPathError: If path is invalid
        """
        await self._http.put(
            f"{self._base_path}/filesystem/files",
            json={"path": path, "content": content},
        )

    async def list_dir(self, path: str = ".") -> list[FileInfo]:
        """List directory contents.

        Args:
            path: Directory path relative to /workspace (default: ".")

        Returns:
            List of FileInfo objects

        Raises:
            CargoFileNotFoundError: If directory doesn't exist
            InvalidPathError: If path is invalid
        """
        response = await self._http.get(
            f"{self._base_path}/filesystem/directories",
            params={"path": path},
        )
        entries = response.get("entries", [])
        return [FileInfo.model_validate(e) for e in entries]

    async def delete(self, path: str) -> None:
        """Delete a file or directory.

        Args:
            path: File/directory path relative to /workspace

        Raises:
            CargoFileNotFoundError: If path doesn't exist
            InvalidPathError: If path is invalid
        """
        await self._http.delete(
            f"{self._base_path}/filesystem/files",
            params={"path": path},
        )

    async def upload(self, path: str, content: bytes) -> None:
        """Upload a binary file to the sandbox.

        Uses multipart/form-data internally.

        Args:
            path: Target path relative to /workspace
            content: Binary file content

        Raises:
            InvalidPathError: If path is invalid
        """
        await self._http.upload(
            f"{self._base_path}/filesystem/upload",
            file_content=content,
            file_path=path,
        )

    async def download(self, path: str) -> bytes:
        """Download a file as binary content.

        Args:
            path: File path relative to /workspace

        Returns:
            Binary file content

        Raises:
            CargoFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
        """
        return await self._http.download(
            f"{self._base_path}/filesystem/download",
            params={"path": path},
        )
