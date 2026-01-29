"""FastAPI dependencies for Bay API.

Provides dependency injection for:
- Database sessions
- Managers (Sandbox, Session, Workspace)
- Driver
- Services (Idempotency)
- Authentication
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_session_dependency
from app.drivers.base import Driver
from app.drivers.docker import DockerDriver
from app.errors import UnauthorizedError
from app.managers.sandbox import SandboxManager
from app.services.idempotency import IdempotencyService


@lru_cache
def get_driver() -> Driver:
    """Get cached driver instance.
    
    Uses lru_cache to ensure single driver instance across requests.
    """
    settings = get_settings()
    if settings.driver.type == "docker":
        return DockerDriver()
    else:
        raise ValueError(f"Unsupported driver type: {settings.driver.type}")


async def get_sandbox_manager(
    session: Annotated[AsyncSession, Depends(get_session_dependency)],
) -> SandboxManager:
    """Get SandboxManager with injected dependencies."""
    driver = get_driver()
    return SandboxManager(driver=driver, db_session=session)


async def get_idempotency_service(
    session: Annotated[AsyncSession, Depends(get_session_dependency)],
) -> IdempotencyService:
    """Get IdempotencyService with injected dependencies."""
    settings = get_settings()
    return IdempotencyService(
        db_session=session,
        config=settings.idempotency,
    )


def authenticate(request: Request) -> str:
    """Authenticate request and return owner.

    Single-tenant mode: Always returns "default" as owner.

    Authentication flow:
    1. If Bearer token provided → validate API key
    2. If no token and allow_anonymous → allow (with optional X-Owner)
    3. Otherwise → 401 Unauthorized

    Returns:
        Owner identifier (currently fixed to "default" for single-tenant)

    Raises:
        UnauthorizedError: If authentication fails
    """
    settings = get_settings()
    security = settings.security
    auth_header = request.headers.get("Authorization")

    # 1. Bearer token provided
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

        # Validate API Key (if configured)
        if security.api_key:
            if token == security.api_key:
                return "default"  # Single-tenant, fixed owner
            raise UnauthorizedError("Invalid API key")

        # No API key configured, accept any token in anonymous mode
        if security.allow_anonymous:
            return "default"
        raise UnauthorizedError("Authentication required")

    # 2. No token - check anonymous mode
    if security.allow_anonymous:
        # Development mode: allow X-Owner header for testing
        owner = request.headers.get("X-Owner")
        if owner:
            return owner
        return "default"

    # 3. Production mode, authentication required
    raise UnauthorizedError("Authentication required")


# Type aliases for cleaner dependency injection
DriverDep = Annotated[Driver, Depends(get_driver)]
SessionDep = Annotated[AsyncSession, Depends(get_session_dependency)]
SandboxManagerDep = Annotated[SandboxManager, Depends(get_sandbox_manager)]
IdempotencyServiceDep = Annotated[IdempotencyService, Depends(get_idempotency_service)]
AuthDep = Annotated[str, Depends(authenticate)]
