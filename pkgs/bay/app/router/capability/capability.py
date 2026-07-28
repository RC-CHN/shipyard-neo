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
from app.validators.path import normalize_sandbox_path, path_for_runtime

logger = structlog.get_logger()


class CapabilityRouter:
    """Routes capability requests to the appropriate runtime adapter."""

    def __init__(
        self,
        sandbox_mgr: SandboxManager,
        *,
        adapter_pool: AdapterPool[BaseAdapter] | None = None,
        shared_gull: BaseAdapter | None = None,
    ) -> None:
        self._sandbox_mgr = sandbox_mgr
        self._log = logger.bind(component="capability_router")
        self._adapter_pool = default_adapter_pool if adapter_pool is None else adapter_pool

        # Auto-detect shared Gull from config if not explicitly passed
        if shared_gull is None:
            try:
                from app.adapters.shared_gull import SharedGullAdapter
                from app.config import get_settings

                settings = get_settings()
                if (
                    hasattr(settings, "browser_service")
                    and settings.browser_service
                    and getattr(settings.browser_service, "enabled", False)
                ):
                    endpoint = settings.browser_service.endpoint
                    self._log.info("shared_gull.enabled", endpoint=endpoint)
                    shared_gull = SharedGullAdapter(endpoint)
            except Exception:
                pass  # Not configured or import error — use per-sandbox mode

        self._shared_gull = shared_gull

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

        # Use endpoint + runtime_type as cache key to prevent stale adapter
        # when Docker reassigns a host port previously used by a different
        # runtime type (e.g., Ship port recycled to Gull).
        pool_key = f"{final_endpoint}::{final_runtime_type}"
        return self._adapter_pool.get_or_create(pool_key, factory)

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

    async def _path_for_runtime(
        self,
        adapter: BaseAdapter,
        path: str,
        *,
        field_name: str,
    ) -> str:
        """Use one normalization path before negotiating Ship compatibility."""
        from app.config import get_settings

        normalized_path = normalize_sandbox_path(
            path,
            allowed_roots=get_settings().filesystem.allowed_roots,
            field_name=field_name,
        )
        meta = await adapter.get_meta()
        policy = meta.path_policy
        return path_for_runtime(
            normalized_path,
            accepts_absolute_paths=policy.accepts_absolute_paths if policy else False,
            allowed_roots=policy.allowed_roots if policy else ("/workspace",),
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
            cwd: Working directory following the public sandbox path policy

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

        runtime_cwd = (
            await self._path_for_runtime(adapter, cwd, field_name="cwd")
            if cwd is not None
            else None
        )
        return await adapter.exec_shell(command, timeout=timeout, cwd=runtime_cwd)

    # -- Browser capability (Phase 2) --

    async def exec_browser(
        self,
        sandbox: Sandbox,
        cmd: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """Execute browser automation command in sandbox.

        Routes to shared Gull Service when available (browser:shared profile),
        otherwise uses per-sandbox Gull container.
        """
        # Shared browser path: no container needed
        if self._shared_gull is not None:
            self._log.info(
                "capability.browser.exec_shared",
                sandbox_id=sandbox.id,
                cmd=cmd[:100],
            )
            return await self._shared_gull.exec_browser(
                cmd,
                sandbox_id=sandbox.id,
                cargo_id=sandbox.cargo_id,
                timeout=timeout,
            )

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

    async def exec_browser_batch(
        self,
        sandbox: Sandbox,
        commands: list[str],
        *,
        timeout: int = 60,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Execute a batch of browser automation commands in sandbox.

        Routes to shared Gull Service when available, otherwise per-sandbox.
        """
        # Shared browser path
        if self._shared_gull is not None:
            self._log.info(
                "capability.browser.exec_batch_shared",
                sandbox_id=sandbox.id,
                n_cmds=len(commands),
            )
            results = await self._shared_gull.exec_browser_batch(
                commands,
                sandbox_id=sandbox.id,
                cargo_id=sandbox.cargo_id,
                timeout=timeout,
                stop_on_error=stop_on_error,
            )
            return {
                "results": [
                    {
                        "cmd": c,
                        "stdout": r.output,
                        "stderr": r.error,
                        "exit_code": r.exit_code,
                        "step_index": i,
                        "duration_ms": 0,
                    }
                    for i, (c, r) in enumerate(zip(commands, results))
                ],
                "total_steps": len(commands),
                "completed_steps": len(results),
                "success": all(r.success for r in results),
                "duration_ms": 0,
            }

        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="browser")
        await self._require_capability(adapter, "browser")

        self._log.info(
            "capability.browser.exec_batch",
            sandbox_id=sandbox.id,
            session_id=session.id,
            num_commands=len(commands),
        )

        return await adapter.exec_browser_batch(
            commands,
            timeout=timeout,
            stop_on_error=stop_on_error,
        )

    # -- Filesystem capability --

    async def read_file(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> str:
        """Read file content from sandbox.

        Args:
            sandbox: Target sandbox
            path: File path following the public sandbox path policy

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

        runtime_path = await self._path_for_runtime(adapter, path, field_name="path")
        return await adapter.read_file(runtime_path)

    async def write_file(
        self,
        sandbox: Sandbox,
        path: str,
        content: str,
    ) -> None:
        """Write file content to sandbox.

        Args:
            sandbox: Target sandbox
            path: File path following the public sandbox path policy
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

        runtime_path = await self._path_for_runtime(adapter, path, field_name="path")
        await adapter.write_file(runtime_path, content)

    async def list_files(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> list[dict[str, Any]]:
        """List directory contents in sandbox.

        Args:
            sandbox: Target sandbox
            path: Directory path following the public sandbox path policy

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

        runtime_path = await self._path_for_runtime(adapter, path, field_name="path")
        return await adapter.list_files(runtime_path)

    async def delete_file(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> None:
        """Delete file or directory from sandbox.

        Args:
            sandbox: Target sandbox
            path: File/directory path following the public sandbox path policy
        """
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="filesystem")
        await self._require_capability(adapter, "filesystem")

        self._log.info(
            "capability.files.delete",
            sandbox_id=sandbox.id,
            path=path,
        )

        runtime_path = await self._path_for_runtime(adapter, path, field_name="path")
        await adapter.delete_file(runtime_path)

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
            path: Target path following the public sandbox path policy
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

        runtime_path = await self._path_for_runtime(adapter, path, field_name="path")
        await adapter.upload_file(runtime_path, content)

    async def download_file(
        self,
        sandbox: Sandbox,
        path: str,
    ) -> bytes:
        """Download file from sandbox.

        Args:
            sandbox: Target sandbox
            path: File path following the public sandbox path policy

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

        runtime_path = await self._path_for_runtime(adapter, path, field_name="path")
        return await adapter.download_file(runtime_path)
