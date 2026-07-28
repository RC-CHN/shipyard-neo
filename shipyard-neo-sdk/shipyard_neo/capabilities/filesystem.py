"""Filesystem capability."""

from __future__ import annotations

from shipyard_neo.capabilities.base import BaseCapability
from shipyard_neo.types import FileInfo


class FilesystemCapability(BaseCapability):
    """Filesystem operations capability.

    Read, write, list, and delete files in the sandbox.

    Relative paths are anchored at ``/workspace``. Absolute paths may use an
    allowed runtime root such as ``/workspace`` or ``/tmp``. ``/tmp`` belongs
    only to the current Ship container; use ``/workspace`` for shared or
    persistent files.
    """

    async def read_file(self, path: str) -> str:
        """Read a text file from the sandbox.

        Args:
            path: Sandbox path relative to /workspace or an allowed absolute path

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
            path: Sandbox path relative to /workspace or an allowed absolute path
            content: File content as string

        Raises:
            InvalidPathError: If path is invalid
        """
        from shipyard_neo.types import _FileWriteRequest

        body = _FileWriteRequest(path=path, content=content).model_dump(exclude_none=True)

        await self._http.put(
            f"{self._base_path}/filesystem/files",
            json=body,
        )

    async def list_dir(self, path: str = ".") -> list[FileInfo]:
        """List directory contents.

        Args:
            path: Sandbox path relative to /workspace or an allowed absolute path

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
            path: Sandbox path relative to /workspace or an allowed absolute path

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
            path: Sandbox path relative to /workspace or an allowed absolute path
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
            path: Sandbox path relative to /workspace or an allowed absolute path

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
