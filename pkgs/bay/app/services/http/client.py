"""Global HTTP client manager for Bay.

Provides a shared httpx.AsyncClient with connection pooling for
efficient HTTP communication with Ship containers.

Benefits:
- Connection reuse across requests (avoids TCP/TLS handshake per request)
- Configurable connection pool limits
- Automatic lifecycle management via FastAPI lifespan
- Reduced memory footprint under high concurrency
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncGenerator

import httpx
import structlog

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger()


class HTTPClientManager:
    """Manages a shared httpx.AsyncClient with connection pooling.
    
    Usage:
        # In FastAPI lifespan
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await http_client_manager.startup()
            yield
            await http_client_manager.shutdown()
        
        # In code
        client = http_client_manager.client
        response = await client.get("http://...")
    """

    def __init__(
        self,
        *,
        max_connections: int = 200,
        max_keepalive_connections: int = 100,
        keepalive_expiry: float = 30.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        write_timeout: float = 30.0,
        pool_timeout: float = 10.0,
    ) -> None:
        """Initialize HTTP client manager.
        
        Args:
            max_connections: Maximum number of concurrent connections.
                             Default 200 to handle high concurrency stress tests.
            max_keepalive_connections: Maximum connections to keep alive.
                                       Should be <= max_connections.
            keepalive_expiry: Seconds before idle connections are closed.
            connect_timeout: Connection timeout in seconds.
            read_timeout: Read timeout in seconds.
                          Default 60s for long-running operations.
            write_timeout: Write timeout in seconds.
            pool_timeout: Pool acquisition timeout in seconds.
        """
        self._max_connections = max_connections
        self._max_keepalive_connections = max_keepalive_connections
        self._keepalive_expiry = keepalive_expiry
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout
        self._pool_timeout = pool_timeout
        
        self._client: httpx.AsyncClient | None = None
        self._log = logger.bind(component="http_client")

    @property
    def client(self) -> httpx.AsyncClient:
        """Get the shared HTTP client.
        
        Raises:
            RuntimeError: If client is not initialized (call startup first)
        """
        if self._client is None:
            raise RuntimeError(
                "HTTP client not initialized. Call startup() first."
            )
        return self._client

    @property
    def is_started(self) -> bool:
        """Check if client is started."""
        return self._client is not None

    async def startup(self) -> None:
        """Initialize the HTTP client with connection pooling."""
        if self._client is not None:
            self._log.warning("http_client.already_started")
            return

        # Configure connection limits
        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_keepalive_connections,
            keepalive_expiry=self._keepalive_expiry,
        )

        # Configure timeouts
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._write_timeout,
            pool=self._pool_timeout,
        )

        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            # Disable HTTP/2 for simpler connection management
            http2=False,
        )

        self._log.info(
            "http_client.started",
            max_connections=self._max_connections,
            max_keepalive=self._max_keepalive_connections,
        )

    async def shutdown(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client is None:
            return

        await self._client.aclose()
        self._client = None
        self._log.info("http_client.shutdown")


# Global singleton instance
http_client_manager = HTTPClientManager()


@asynccontextmanager
async def lifespan_http_client(app: "FastAPI") -> AsyncGenerator[None, None]:
    """FastAPI lifespan context manager for HTTP client.
    
    Usage in main.py:
        from app.services.http import lifespan_http_client
        
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            async with lifespan_http_client(app):
                yield
    """
    await http_client_manager.startup()
    try:
        yield
    finally:
        await http_client_manager.shutdown()


def get_http_client() -> httpx.AsyncClient:
    """Get the shared HTTP client (dependency injection helper).
    
    Use this in FastAPI dependencies or anywhere you need the client.
    
    Returns:
        Shared httpx.AsyncClient
        
    Raises:
        RuntimeError: If client not initialized
    """
    return http_client_manager.client
