"""HTTP client wrapper for Bay API.

Handles connection pooling, error mapping, and request/response serialization.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any

import httpx

from shipyard_neo.errors import raise_for_error_response

logger = logging.getLogger("shipyard_neo")


class HTTPClient:
    """Async HTTP client for Bay API.

    Wraps httpx.AsyncClient with:
    - Connection pooling
    - Automatic error response mapping to BayError
    - Idempotency-Key header support
    - Request/response logging
    """

    def __init__(
        self,
        base_url: str,
        access_token: str,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        """Initialize HTTP client.

        Args:
            base_url: Bay API base URL (e.g., "http://localhost:8000")
            access_token: Bearer token for authentication
            timeout: Default request timeout in seconds
            max_retries: Maximum retry attempts for transient errors
        """
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _is_retryable_method(method: str, *, has_idempotency_key: bool) -> bool:
        method_upper = method.upper()
        if method_upper in {"GET", "PUT", "DELETE"}:
            return True
        return method_upper == "POST" and has_idempotency_key

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    @staticmethod
    def _retry_delay_seconds(attempt: int) -> float:
        # attempt is zero-based retry attempt index
        return min(0.2 * (2**attempt), 1.5)

    @staticmethod
    def _parse_json_or_error_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            return {"data": payload}
        except Exception:
            if response.status_code >= 400:
                raw_text = response.text or ""
                snippet_limit = 500
                snippet = raw_text[:snippet_limit]
                return {
                    "error": {
                        "message": f"HTTP {response.status_code} returned non-JSON error response",
                        "details": {
                            "raw_response_snippet": snippet,
                            "raw_response_truncated": len(raw_text) > snippet_limit,
                        },
                    }
                }
            return {}

    async def __aenter__(self) -> HTTPClient:
        """Enter async context, creating HTTP client."""
        # Don't set Content-Type here; set it per-request
        # This allows multipart uploads to set their own content type
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={
                "Authorization": f"Bearer {self._access_token}",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context, closing HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get the underlying httpx client."""
        if self._client is None:
            raise RuntimeError("HTTPClient not initialized. Use 'async with' context.")
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to Bay API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            path: API path (e.g., "/v1/sandboxes")
            json: Request body as dict (will be serialized)
            params: Query parameters
            idempotency_key: Optional idempotency key header
            timeout: Override default timeout for this request

        Returns:
            Parsed JSON response body

        Raises:
            BayError: On API error responses
        """
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        # Set Content-Type for JSON requests
        if json is not None:
            headers["Content-Type"] = "application/json"

        # Filter None values from params
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        logger.debug("Request: %s %s", method, path)

        has_idempotency_key = idempotency_key is not None
        retryable_method = self._is_retryable_method(
            method,
            has_idempotency_key=has_idempotency_key,
        )
        max_attempts = self._max_retries + 1 if retryable_method else 1

        for attempt in range(max_attempts):
            try:
                response = await self.client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers=headers if headers else None,
                    timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                if retryable_method and attempt < max_attempts - 1:
                    await asyncio.sleep(self._retry_delay_seconds(attempt))
                    continue
                raise

            logger.debug("Response: %s %s", response.status_code, path)

            if response.status_code == 204:
                return {}

            # Retry on transient HTTP status for retryable methods.
            if (
                retryable_method
                and attempt < max_attempts - 1
                and self._is_retryable_status(response.status_code)
            ):
                await asyncio.sleep(self._retry_delay_seconds(attempt))
                continue

            body = self._parse_json_or_error_payload(response)
            if response.status_code >= 400:
                raise_for_error_response(response.status_code, body)
            return body

        # Defensive fallback, loop should always return/raise.
        raise RuntimeError("HTTP request attempt loop exhausted unexpectedly")

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make a GET request."""
        return await self.request("GET", path, params=params, timeout=timeout)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make a POST request."""
        return await self.request(
            "POST",
            path,
            json=json,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    async def put(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make a PUT request."""
        return await self.request("PUT", path, json=json, timeout=timeout)

    async def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make a DELETE request."""
        return await self.request("DELETE", path, params=params, timeout=timeout)

    async def upload(
        self,
        path: str,
        *,
        file_content: bytes,
        file_path: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Upload a file via multipart/form-data.

        Args:
            path: API path
            file_content: Binary file content
            file_path: Target path in sandbox workspace
            timeout: Override default timeout

        Returns:
            Parsed JSON response
        """
        files = {"file": ("upload", file_content, "application/octet-stream")}
        data = {"path": file_path}

        # httpx automatically sets Content-Type: multipart/form-data with boundary
        response = await self.client.post(
            path,
            files=files,
            data=data,
            timeout=timeout,
        )

        body = self._parse_json_or_error_payload(response)
        if response.status_code >= 400:
            raise_for_error_response(response.status_code, body)
        return body

    async def download(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Download a file as binary content.

        Args:
            path: API path
            params: Query parameters
            timeout: Override default timeout

        Returns:
            Binary file content
        """
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        retryable_method = self._is_retryable_method("GET", has_idempotency_key=False)
        max_attempts = self._max_retries + 1 if retryable_method else 1

        for attempt in range(max_attempts):
            try:
                response = await self.client.get(
                    path,
                    params=params,
                    timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                if retryable_method and attempt < max_attempts - 1:
                    await asyncio.sleep(self._retry_delay_seconds(attempt))
                    continue
                raise

            if (
                retryable_method
                and attempt < max_attempts - 1
                and self._is_retryable_status(response.status_code)
            ):
                await asyncio.sleep(self._retry_delay_seconds(attempt))
                continue

            if response.status_code >= 400:
                body = self._parse_json_or_error_payload(response)
                raise_for_error_response(response.status_code, body)

            return response.content

        raise RuntimeError("HTTP download attempt loop exhausted unexpectedly")
