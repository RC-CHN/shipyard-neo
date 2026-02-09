"""Gull runtime adapter.

HTTP adapter for communicating with Gull runtime containers.

Gull exposes a minimal REST API:
- POST /exec: Execute agent-browser CLI command (passthrough)
- GET /health: Health check
- GET /meta: Runtime metadata

Capability mapping:
- Bay capability "browser" -> Gull /exec

See: [`plans/phase-2/browser-integration-design.md`](plans/phase-2/browser-integration-design.md)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.adapters.base import BaseAdapter, ExecutionResult, RuntimeMeta
from app.errors import RequestTimeoutError, ShipError
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


class GullAdapter(BaseAdapter):
    """HTTP adapter for Gull runtime."""

    SUPPORTED_CAPABILITIES = [
        "browser",
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
        self._log = logger.bind(adapter="browser", base_url=base_url)

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
        url = f"{self._base_url}{path}"
        request_timeout = timeout or self._timeout

        try:
            client = _get_shared_client()
            if client is not None:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    timeout=request_timeout,
                )
            else:
                async with httpx.AsyncClient(trust_env=False) as temp_client:
                    response = await temp_client.request(
                        method,
                        url,
                        json=json,
                        timeout=request_timeout,
                    )

            if response.status_code >= 400:
                self._log.error(
                    "gull.request_failed",
                    path=path,
                    status=response.status_code,
                    body=response.text,
                )
                raise ShipError(f"Gull request failed: {response.status_code}")

            return response.json()

        except httpx.TimeoutException:
            self._log.error("gull.timeout", path=path, timeout=request_timeout)
            raise RequestTimeoutError(f"Gull request timed out: {path}")
        except httpx.RequestError as e:
            self._log.error("gull.request_error", path=path, error=str(e))
            raise ShipError(f"Gull request error: {e}")

    async def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, json: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", path, json=json, **kwargs)

    async def get_meta(self) -> RuntimeMeta:
        if self._meta_cache is not None:
            return self._meta_cache

        data = await self._get("/meta", timeout=5.0)

        runtime = data.get("runtime", {})
        workspace = data.get("workspace", {})
        capabilities = data.get("capabilities", {})

        self._meta_cache = RuntimeMeta(
            name=runtime.get("name", "gull"),
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
        try:
            client = _get_shared_client()
            if client is not None:
                response = await client.get(
                    f"{self._base_url}/health",
                    timeout=5.0,
                )
            else:
                async with httpx.AsyncClient(trust_env=False) as temp_client:
                    response = await temp_client.get(
                        f"{self._base_url}/health",
                        timeout=5.0,
                    )
            return response.status_code == 200
        except Exception:
            return False

    async def exec_browser(
        self,
        cmd: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """Execute an agent-browser command via Gull.

        Args:
            cmd: agent-browser command without prefix (e.g., "open https://example.com")
            timeout: timeout seconds

        Returns:
            ExecutionResult: stdout in output, stderr in error.
        """
        result = await self._post(
            "/exec",
            {"cmd": cmd, "timeout": timeout},
            timeout=float(timeout + 5),
        )

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code")
        try:
            exit_code_int = int(exit_code) if exit_code is not None else None
        except Exception:
            exit_code_int = None

        success = (exit_code_int == 0) if exit_code_int is not None else False

        return ExecutionResult(
            success=success,
            output=stdout,
            error=stderr or None,
            exit_code=exit_code_int,
            data={"raw": result},
        )
