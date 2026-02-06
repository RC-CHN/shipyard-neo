"""Shell execution capability."""

from __future__ import annotations

from shipyard_neo.capabilities.base import BaseCapability
from shipyard_neo.types import ShellExecResult


class ShellCapability(BaseCapability):
    """Shell command execution capability.

    Executes shell commands in the sandbox.
    """

    async def exec(
        self,
        command: str,
        *,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> ShellExecResult:
        """Execute a shell command in the sandbox.

        Args:
            command: Shell command to execute
            timeout: Execution timeout in seconds (1-300)
            cwd: Working directory relative to /workspace

        Returns:
            ShellExecResult with output, error, and exit code

        Raises:
            SessionNotReadyError: If session is still starting
            RequestTimeoutError: If execution times out
            ShipError: If runtime error occurs
            InvalidPathError: If cwd is invalid
        """
        body: dict = {
            "command": command,
            "timeout": timeout,
        }
        if cwd is not None:
            body["cwd"] = cwd

        response = await self._http.post(
            f"{self._base_path}/shell/exec",
            json=body,
            timeout=float(timeout) + 10,
        )

        return ShellExecResult.model_validate(response)
