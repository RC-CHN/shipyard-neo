"""Python execution capability."""

from __future__ import annotations

from shipyard_neo.capabilities.base import BaseCapability
from shipyard_neo.types import PythonExecResult


class PythonCapability(BaseCapability):
    """Python code execution capability.

    Executes Python code in the sandbox's IPython kernel.
    Variables persist across calls within the same session.
    """

    async def exec(
        self,
        code: str,
        *,
        timeout: int = 30,
        include_code: bool = False,
        description: str | None = None,
        tags: str | None = None,
    ) -> PythonExecResult:
        """Execute Python code in the sandbox.

        Args:
            code: Python code to execute
            timeout: Execution timeout in seconds (1-300)
            include_code: Include executed code in response payload
            description: Optional execution description to store in history
            tags: Optional comma-separated tags to store in history

        Returns:
            PythonExecResult with output, error, and rich data

        Raises:
            SessionNotReadyError: If session is still starting
            RequestTimeoutError: If execution times out
            ShipError: If runtime error occurs
        """
        response = await self._http.post(
            f"{self._base_path}/python/exec",
            json={
                "code": code,
                "timeout": timeout,
                "include_code": include_code,
                "description": description,
                "tags": tags,
            },
            timeout=float(timeout) + 10,  # Add buffer for network overhead
        )

        return PythonExecResult.model_validate(response)
