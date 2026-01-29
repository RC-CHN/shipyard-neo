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
from app.adapters.ship import ShipAdapter
from app.errors import SessionNotReadyError
from app.managers.sandbox import SandboxManager
from app.models.sandbox import Sandbox
from app.models.session import Session

logger = structlog.get_logger()


class CapabilityRouter:
    """Routes capability requests to the appropriate runtime adapter."""

    def __init__(self, sandbox_mgr: SandboxManager) -> None:
        self._sandbox_mgr = sandbox_mgr
        self._log = logger.bind(component="capability_router")
        # Cache of adapters by endpoint
        self._adapters: dict[str, BaseAdapter] = {}

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

    def _get_adapter(self, session: Session) -> BaseAdapter:
        """Get or create adapter for session.
        
        Caches adapters by endpoint to avoid creating new instances.
        """
        if session.endpoint is None:
            raise SessionNotReadyError(
                message="Session has no endpoint",
                sandbox_id=session.sandbox_id,
            )

        if session.endpoint not in self._adapters:
            # Create adapter based on runtime type
            if session.runtime_type == "ship":
                self._adapters[session.endpoint] = ShipAdapter(session.endpoint)
            else:
                raise ValueError(f"Unknown runtime type: {session.runtime_type}")

        return self._adapters[session.endpoint]

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
        adapter = self._get_adapter(session)

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
        adapter = self._get_adapter(session)

        self._log.info(
            "capability.shell.exec",
            sandbox_id=sandbox.id,
            session_id=session.id,
            command=command[:100],
        )

        return await adapter.exec_shell(command, timeout=timeout, cwd=cwd)

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
        adapter = self._get_adapter(session)

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
        adapter = self._get_adapter(session)

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
        adapter = self._get_adapter(session)

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
        adapter = self._get_adapter(session)

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
        adapter = self._get_adapter(session)

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
        adapter = self._get_adapter(session)

        self._log.info(
            "capability.files.download",
            sandbox_id=sandbox.id,
            path=path,
        )

        return await adapter.download_file(path)
