"""CapabilityRouter - routes capability requests to runtime adapters.

Responsibilities:
- Resolve sandbox_id -> session endpoint
- Ensure session is running (ensure_running)
- Apply policies: timeout, retry, circuit-breaker, audit
- Route to appropriate RuntimeAdapter

See: plans/phase-1/capability-adapter-design.md
"""

from __future__ import annotations

from typing import Any

import structlog

from app.adapters.base import BaseAdapter, ExecutionResult
from app.adapters.gull import GullAdapter
from app.adapters.ship import ShipAdapter
from app.errors import CapabilityNotSupportedError, SessionNotReadyError
from app.managers.sandbox import SandboxManager
from app.models.sandbox import Sandbox
from app.models.session import Session
from app.router.capability.adapter_pool import AdapterPool, default_adapter_pool

logger = structlog.get_logger()


class CapabilityRouter:
    """Routes capability requests to the appropriate runtime adapter."""

    def __init__(
        self,
        sandbox_mgr: SandboxManager,
        *,
        adapter_pool: AdapterPool[BaseAdapter] | None = None,
    ) -> None:
        self._sandbox_mgr = sandbox_mgr
        self._log = logger.bind(component="capability_router")
        self._adapter_pool = default_adapter_pool if adapter_pool is None else adapter_pool

    async def ensure_session(self, sandbox: Sandbox) -> Session:
        """Ensure sandbox has a running session.

        Args:
            sandbox: Sandbox to ensure is running

        Returns:
            Running session

        Raises:
            SessionNotReadyError: If session is starting
        """
        return await self._sandbox_mgr.ensure_running(sandbox)

    def _get_adapter(self, session: Session, *, capability: str | None = None) -> BaseAdapter:
        """Get or create adapter for session.

        Phase 2:
        - If capability is specified and session is multi-container, route to the
          correct container endpoint.
        - Routing priority follows ProfileConfig rules when profile is available:
          [`ProfileConfig.find_container_for_capability()`](pkgs/bay/app/config.py:234)
          (primary_for wins, then first capabilities match).
        - If profile is missing, falls back to first matching container in
          [`Session.containers`](pkgs/bay/app/models/session.py:76).

        Otherwise falls back to the primary container (backward compatible).

        Caches adapters by endpoint to avoid creating new instances.

        Args:
            session: Session to get adapter for
            capability: Optional capability to route to the correct container
        """
        endpoint: str | None = None
        runtime_type: str = session.runtime_type

        # Phase 2: Multi-container routing
        if capability and session.is_multi_container and session.containers:
            # Prefer routing via profile (supports primary_for semantics)
            profile = None
            try:
                # Local import to avoid creating a hard dependency at module import time
                from app.config import get_settings

                profile = get_settings().get_profile(session.profile_id)
            except Exception:
                profile = None

            target_name: str | None = None
            if profile is not None:
                spec = profile.find_container_for_capability(capability)
                if spec is not None:
                    target_name = spec.name

            container_dict = None
            if target_name is not None:
                for c in session.containers:
                    if c.get("name") == target_name:
                        container_dict = c
                        break

            # Fallback: first container that declares capability (order preserved)
            if container_dict is None:
                container_dict = session.get_container_for_capability(capability)

            if container_dict:
                endpoint = container_dict.get("endpoint")
                runtime_type = container_dict.get("runtime_type", "ship")
            else:
                # Capability not found in any container
                raise CapabilityNotSupportedError(
                    message=f"No container provides capability: {capability}",
                    capability=capability,
                    available=self._get_all_session_capabilities(session),
                )

        # Fallback to primary container endpoint
        if endpoint is None:
            endpoint = session.endpoint

        if endpoint is None:
            raise SessionNotReadyError(
                message="Session has no endpoint",
                sandbox_id=session.sandbox_id,
            )

        final_endpoint = endpoint
        final_runtime_type = runtime_type

        def factory() -> BaseAdapter:
            if final_runtime_type == "ship":
                return ShipAdapter(final_endpoint)
            if final_runtime_type == "gull":
                return GullAdapter(final_endpoint)
            raise ValueError(f"Unknown runtime type: {final_runtime_type}")

        return self._adapter_pool.get_or_create(final_endpoint, factory)

    @staticmethod
    def _get_all_session_capabilities(session: Session) -> list[str]:
        """Get all capabilities from all containers in a session."""
        if not session.containers:
            return []
        caps: set[str] = set()
        for c in session.containers:
            caps.update(c.get("capabilities", []))
        return sorted(caps)

    async def _require_capability(self, adapter: BaseAdapter, capability: str) -> None:
        """Fail-fast if runtime does not declare the requested capability.

        Uses runtime `/meta` (cached by adapter) to validate.
        """
        meta = await adapter.get_meta()
        if capability not in meta.capabilities:
            raise CapabilityNotSupportedError(
                message=f"Runtime does not support capability: {capability}",
                capability=capability,
                available=list(meta.capabilities.keys()),
            )

    # -- Python capability --

    async def exec_python(
        self,
        sandbox: Sandbox,
        code: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """Execute Python code in sandbox.

        Args:
            sandbox: Target sandbox
            code: Python code to execute
            timeout: Execution timeout in seconds

        Returns:
            Execution result
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="python")
        await self._require_capability(adapter, "python")

        self._log.info(
            "capability.python.exec",
            sandbox_id=sandbox.id,
            session_id=session.id,
            code_len=len(code),
        )

        return await adapter.exec_python(code, timeout=timeout)

    # -- Shell capability --

    async def exec_shell(
        self,
        sandbox: Sandbox,
        command: str,
        *,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> ExecutionResult:
        """Execute shell command in sandbox.

        Args:
            sandbox: Target sandbox
            command: Shell command to execute
            timeout: Execution timeout in seconds
            cwd: Working directory (relative to /workspace)

        Returns:
            Execution result
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="shell")
        await self._require_capability(adapter, "shell")

        self._log.info(
            "capability.shell.exec",
            sandbox_id=sandbox.id,
            session_id=session.id,
            command=command[:100],
        )

        return await adapter.exec_shell(command, timeout=timeout, cwd=cwd)

    # -- Browser capability (Phase 2) --

    async def exec_browser(
        self,
        sandbox: Sandbox,
        cmd: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """Execute browser automation command in sandbox.

        Phase 2: Routes to the Gull container via CLI passthrough.

        Args:
            sandbox: Target sandbox
            cmd: agent-browser command (without prefix)
            timeout: Execution timeout in seconds

        Returns:
            Execution result
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="browser")
        await self._require_capability(adapter, "browser")

        self._log.info(
            "capability.browser.exec",
            sandbox_id=sandbox.id,
            session_id=session.id,
            cmd=cmd[:100],
        )

        return await adapter.exec_browser(cmd, timeout=timeout)

    # -- Filesystem capability --

    async def read_file(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> str:
        """Read file content from sandbox.

        Args:
            sandbox: Target sandbox
            path: File path (relative to /workspace)

        Returns:
            File content
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.read",
            sandbox_id=sandbox.id,
            path=path,
        )

        return await adapter.read_file(path)

    async def write_file(
        self,
        sandbox: Sandbox,
        path: str,
        content: str,
    ) -> None:
        """Write file content to sandbox.

        Args:
            sandbox: Target sandbox
            path: File path (relative to /workspace)
            content: File content
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.write",
            sandbox_id=sandbox.id,
            path=path,
            content_len=len(content),
        )

        await adapter.write_file(path, content)

    async def list_files(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> list[dict[str, Any]]:
        """List directory contents in sandbox.

        Args:
            sandbox: Target sandbox
            path: Directory path (relative to /workspace)

        Returns:
            List of file entries
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.list",
            sandbox_id=sandbox.id,
            path=path,
        )

        return await adapter.list_files(path)

    async def delete_file(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> None:
        """Delete file or directory from sandbox.

        Args:
            sandbox: Target sandbox
            path: File/directory path (relative to /workspace)
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.delete",
            sandbox_id=sandbox.id,
            path=path,
        )

        await adapter.delete_file(path)

    # -- Upload/Download capability --

    async def upload_file(
        self,
        sandbox: Sandbox,
        path: str,
        content: bytes,
    ) -> None:
        """Upload binary file to sandbox.

        Args:
            sandbox: Target sandbox
            path: Target path (relative to /workspace)
            content: File content as bytes
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.upload",
            sandbox_id=sandbox.id,
            path=path,
            content_len=len(content),
        )

        await adapter.upload_file(path, content)

    async def download_file(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> bytes:
        """Download file from sandbox.

        Args:
            sandbox: Target sandbox
            path: File path (relative to /workspace)

        Returns:
            File content as bytes
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.download",
            sandbox_id=sandbox.id,
            path=path,
        )

        return await adapter.download_file(path)
