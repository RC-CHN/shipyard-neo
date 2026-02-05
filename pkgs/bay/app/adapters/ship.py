"""Ship runtime adapter.

Pure HTTP adapter for communicating with Ship containers.
See: plans/phase-1/capability-adapter-design.md

NOTE: Ship endpoint mappings:
- python capability -> /ipython/exec (Ship uses IPython internally)
- shell capability -> /shell/exec
- filesystem capability -> /fs/* (includes upload/download)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.adapters.base import BaseAdapter, ExecutionResult, RuntimeMeta
from app.errors import CargoFileNotFoundError, RequestTimeoutError, ShipError
from app.services.http import http_client_manager

logger = structlog.get_logger()


def _get_shared_client() -> httpx.AsyncClient | None:
    """Get shared HTTP client if available.
    
    Returns None if client manager is not initialized (e.g., in tests).
    """
    try:
        return http_client_manager.client
    except RuntimeError:
        return None


class ShipAdapter(BaseAdapter):
    """HTTP adapter for Ship runtime.

    Supports connection pooling via shared HTTP client for better performance.
    Supported capabilities: python, shell, filesystem (includes upload/download), terminal
    """

    SUPPORTED_CAPABILITIES = [
        "python",
        "shell",
        "filesystem",
        "terminal",
    ]

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._meta_cache: RuntimeMeta | None = None
        self._log = logger.bind(adapter="ship", base_url=base_url)

    def supported_capabilities(self) -> list[str]:
        return self.SUPPORTED_CAPABILITIES

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request to Ship using shared connection pool."""
        url = f"{self._base_url}{path}"
        request_timeout = timeout or self._timeout

        try:
            # Use shared client for connection pooling
            client = _get_shared_client()
            if client is not None:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    timeout=request_timeout,
                )
            else:
                # Fallback: create temporary client (for tests)
                async with httpx.AsyncClient(trust_env=False) as temp_client:
                    response = await temp_client.request(
                        method,
                        url,
                        json=json,
                        timeout=request_timeout,
                    )

            if response.status_code >= 400:
                self._log.error(
                    "ship.request_failed",
                    path=path,
                    status=response.status_code,
                    body=response.text,
                )
                raise ShipError(f"Ship request failed: {response.status_code}")

            return response.json()

        except httpx.TimeoutException:
            self._log.error("ship.timeout", path=path, timeout=request_timeout)
            raise RequestTimeoutError(f"Ship request timed out: {path}")
        except httpx.RequestError as e:
            self._log.error("ship.request_error", path=path, error=str(e))
            raise ShipError(f"Ship request error: {e}")

    async def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """GET request."""
        return await self._request("GET", path, **kwargs)

    async def _post(
        self, path: str, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """POST request."""
        return await self._request("POST", path, json=json, **kwargs)

    # -- Meta & Health --

    async def get_meta(self) -> RuntimeMeta:
        """Get runtime metadata (with caching)."""
        if self._meta_cache is not None:
            return self._meta_cache

        data = await self._get("/meta", timeout=5.0)

        runtime = data.get("runtime", {})
        workspace = data.get("workspace", {})
        capabilities = data.get("capabilities", {})

        self._meta_cache = RuntimeMeta(
            name=runtime.get("name", "ship"),
            version=runtime.get("version", "unknown"),
            api_version=runtime.get("api_version", "v1"),
            mount_path=workspace.get("mount_path", "/workspace"),
            capabilities=capabilities,
        )

        self._log.info(
            "adapter.meta_cached",
            name=self._meta_cache.name,
            version=self._meta_cache.version,
            capabilities=list(capabilities.keys()),
        )

        return self._meta_cache

    async def health(self) -> bool:
        """Check runtime health using shared connection pool."""
        try:
            client = _get_shared_client()
            if client is not None:
                response = await client.get(
                    f"{self._base_url}/health",
                    timeout=5.0,
                )
            else:
                # Fallback for tests
                async with httpx.AsyncClient(trust_env=False) as temp_client:
                    response = await temp_client.get(
                        f"{self._base_url}/health",
                        timeout=5.0,
                    )
            return response.status_code == 200
        except Exception:
            return False

    # -- Python capability --
    # NOTE: Bay's "python" capability maps to Ship's /ipython/exec endpoint

    async def exec_python(
        self,
        code: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """Execute Python code via Ship's IPython kernel."""
        result = await self._post(
            "/ipython/exec",
            {"code": code, "timeout": timeout, "silent": False},
            timeout=timeout + 5,
        )

        output_obj = result.get("output") or {}
        output_text = output_obj.get("text", "") if isinstance(output_obj, dict) else ""

        return ExecutionResult(
            success=bool(result.get("success", False)),
            output=output_text,
            error=result.get("error"),
            data={
                "execution_count": result.get("execution_count"),
                # Full output object including images:
                # { "text": "...", "images": [{"image/png": "base64..."}] }
                "output": output_obj,
            },
        )

    # -- Shell capability --

    async def exec_shell(
        self,
        command: str,
        *,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> ExecutionResult:
        """Execute shell command."""
        payload: dict[str, Any] = {
            "command": command,
            "timeout": timeout,
        }
        if cwd:
            payload["cwd"] = cwd

        result = await self._post("/shell/exec", payload, timeout=timeout + 5)

        # Ship returns: success, return_code, stdout, stderr, pid, process_id, error
        # Map to ExecutionResult fields
        return ExecutionResult(
            success=result.get("success", False),
            output=result.get("stdout", ""),
            error=result.get("error") or result.get("stderr") or None,
            exit_code=result.get("return_code"),
            data={"raw": result},
        )

    # -- Filesystem capability --

    async def read_file(self, path: str) -> str:
        """Read file content."""
        result = await self._post("/fs/read_file", {"path": path})
        # Ship returns {content, path, size}
        return result.get("content", "")

    async def write_file(self, path: str, content: str) -> None:
        """Write file content."""
        await self._post("/fs/write_file", {"path": path, "content": content, "mode": "w"})

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        """List directory contents."""
        # Ship returns {files: [...], current_path: ...}
        result = await self._post("/fs/list_dir", {"path": path, "show_hidden": False})
        return result.get("files", [])

    async def delete_file(self, path: str) -> None:
        """Delete file or directory."""
        await self._post("/fs/delete_file", {"path": path})

    # -- Upload/Download (part of filesystem capability) --

    async def upload_file(self, path: str, content: bytes) -> None:
        """Upload binary file using shared connection pool."""
        try:
            files = {"file": ("file", content, "application/octet-stream")}
            data = {"file_path": path}
            
            client = _get_shared_client()
            if client is not None:
                response = await client.post(
                    f"{self._base_url}/fs/upload",
                    files=files,
                    data=data,
                    timeout=self._timeout,
                )
            else:
                # Fallback for tests
                async with httpx.AsyncClient() as temp_client:
                    response = await temp_client.post(
                        f"{self._base_url}/fs/upload",
                        files=files,
                        data=data,
                        timeout=self._timeout,
                    )
            
            if response.status_code >= 400:
                raise ShipError(f"Upload failed: {response.status_code}")
        except httpx.RequestError as e:
            raise ShipError(f"Upload file failed: {e}")

    async def download_file(self, path: str) -> bytes:
        """Download file as bytes using shared connection pool."""
        try:
            client = _get_shared_client()
            if client is not None:
                response = await client.get(
                    f"{self._base_url}/fs/download",
                    params={"file_path": path},
                    timeout=self._timeout,
                )
            else:
                # Fallback for tests
                async with httpx.AsyncClient() as temp_client:
                    response = await temp_client.get(
                        f"{self._base_url}/fs/download",
                        params={"file_path": path},
                        timeout=self._timeout,
                    )
            
            if response.status_code == 404:
                raise CargoFileNotFoundError(f"File not found: {path}")
            if response.status_code >= 400:
                raise ShipError(f"Download failed: {response.status_code}")
            return response.content
        except httpx.RequestError as e:
            raise ShipError(f"Download file failed: {e}")
