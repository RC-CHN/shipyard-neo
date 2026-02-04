"""HTTP client service package.

Provides shared HTTP client with connection pooling for
efficient communication with Ship containers.
"""

from app.services.http.client import (
    HTTPClientManager,
    get_http_client,
    http_client_manager,
    lifespan_http_client,
)

__all__ = [
    "HTTPClientManager",
    "get_http_client",
    "http_client_manager",
    "lifespan_http_client",
]
