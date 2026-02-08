"""HTTP client wrapper for Bay API.

Handles connection pooling, error mapping, and request/response serialization.
"""

from __future__ import annotations

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

        response = await self.client.request(
            method,
            path,
            json=json,
            params=params,
            headers=headers if headers else None,
            timeout=timeout,
        )

        logger.debug("Response: %s %s", response.status_code, path)

        # Handle empty responses (204 No Content)
        if response.status_code == 204:
            return {}

        # Parse response body
        try:
            body = response.json()
        except Exception:
            body = {}

        # Check for errors
        if response.status_code >= 400:
            raise_for_error_response(response.status_code, body)

        return body

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

        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = {}
            raise_for_error_response(response.status_code, body)

        return response.json()

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

        response = await self.client.get(
            path,
            params=params,
            timeout=timeout,
        )

        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = {}
            raise_for_error_response(response.status_code, body)

        return response.content
