"""Browser automation capability."""

from __future__ import annotations

from shipyard_neo.capabilities.base import BaseCapability
from shipyard_neo.types import BrowserExecResult


class BrowserCapability(BaseCapability):
    """Browser automation capability.

    Executes browser automation commands in the sandbox via the Gull runtime.
    """

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int = 30,
    ) -> BrowserExecResult:
        """Execute a browser automation command in the sandbox.

        Args:
            cmd: Browser automation command to execute
            timeout: Execution timeout in seconds (1-300)

        Returns:
            BrowserExecResult with output, error, and exit code

        Raises:
            CapabilityNotSupportedError: If browser capability not in profile
            SessionNotReadyError: If session is still starting
            RequestTimeoutError: If execution times out
        """
        response = await self._http.post(
            f"{self._base_path}/browser/exec",
            json={
                "cmd": cmd,
                "timeout": timeout,
            },
            timeout=float(timeout) + 10,  # Add buffer for network overhead
        )

        return BrowserExecResult.model_validate(response)
