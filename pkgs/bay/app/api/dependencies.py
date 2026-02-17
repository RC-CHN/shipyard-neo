"""FastAPI dependencies for Bay API.

Provides dependency injection for:
- Database sessions
- Managers (Sandbox, Session, Cargo)
- Driver
- Services (Idempotency)
- Authentication
- Capability checks
"""

from __future__ import annotations

import hmac
from functools import lru_cache
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_session_dependency
from app.drivers.base import Driver
from app.drivers.docker import DockerDriver
from app.drivers.k8s import K8sDriver
from app.errors import CapabilityNotSupportedError, UnauthorizedError
from app.managers.cargo import CargoManager
from app.managers.sandbox import SandboxManager
from app.models.sandbox import Sandbox
from app.services.idempotency import IdempotencyService
from app.services.skills import SkillLifecycleService

logger = structlog.get_logger()


@lru_cache
def get_driver() -> Driver:
    """Get cached driver instance.

    Uses lru_cache to ensure single driver instance across requests.
    """
    settings = get_settings()
    if settings.driver.type == "docker":
        return DockerDriver()
    elif settings.driver.type == "k8s":
        return K8sDriver()
    else:
        raise ValueError(f"Unsupported driver type: {settings.driver.type}")


async def get_sandbox_manager(
    session: Annotated[AsyncSession, Depends(get_session_dependency)],
) -> SandboxManager:
    """Get SandboxManager with injected dependencies."""
    driver = get_driver()
    return SandboxManager(driver=driver, db_session=session)


async def get_cargo_manager(
    session: Annotated[AsyncSession, Depends(get_session_dependency)],
) -> CargoManager:
    """Get CargoManager with injected dependencies."""
    driver = get_driver()
    return CargoManager(driver=driver, db_session=session)


async def get_idempotency_service(
    session: Annotated[AsyncSession, Depends(get_session_dependency)],
) -> IdempotencyService:
    """Get IdempotencyService with injected dependencies."""
    settings = get_settings()
    return IdempotencyService(
        db_session=session,
        config=settings.idempotency,
    )


async def get_skill_lifecycle_service(
    session: Annotated[AsyncSession, Depends(get_session_dependency)],
) -> SkillLifecycleService:
    """Get SkillLifecycleService with injected dependencies."""
    return SkillLifecycleService(db_session=session)


def authenticate(request: Request) -> str:
    """Authenticate request and return owner.

    Authentication flow:
    1. If Bearer token provided:
       a. If DB key hashes loaded → validate via hash lookup
       b. Else if allow_anonymous → allow any token
       c. Otherwise → 401
    2. If no token and allow_anonymous → allow (with optional X-Owner)
    3. Otherwise → 401 Unauthorized

    Returns:
        Owner identifier (e.g., "default")

    Raises:
        UnauthorizedError: If authentication fails
    """
    settings = get_settings()
    security = settings.security
    auth_header = request.headers.get("Authorization")

    # 1. Bearer token provided
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

        # 1a. DB key hash lookup (loaded at startup into app.state)
        api_key_hashes: dict[str, str] = getattr(
            request.app.state, "api_key_hashes", {}
        )
        if api_key_hashes:
            from app.services.api_key import ApiKeyService

            token_hash = ApiKeyService.hash_key(token)
            owner = None
            for stored_hash, stored_owner in api_key_hashes.items():
                if hmac.compare_digest(token_hash, stored_hash):
                    owner = stored_owner
                    break
            if owner:
                logger.debug("auth.success", source="db", owner=owner)
                return owner
            raise UnauthorizedError("Invalid API key")

        # 1c. No API key configured anywhere, accept any token in anonymous mode
        if security.allow_anonymous:
            logger.debug("auth.success", source="anonymous")
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
CargoManagerDep = Annotated[CargoManager, Depends(get_cargo_manager)]
IdempotencyServiceDep = Annotated[IdempotencyService, Depends(get_idempotency_service)]
SkillLifecycleServiceDep = Annotated[SkillLifecycleService, Depends(get_skill_lifecycle_service)]
AuthDep = Annotated[str, Depends(authenticate)]


# ---- Capability enforcement ----


def require_capability(capability: str):
    """Factory for capability check dependency.

    Creates a dependency that validates the sandbox's profile supports
    the requested capability BEFORE starting any container.

    This is Level 1 (Profile) enforcement. Level 2 (Runtime) enforcement
    is done in CapabilityRouter._require_capability().

    Args:
        capability: Capability name (python, shell, filesystem, etc.)

    Returns:
        A dependency function that returns the Sandbox if allowed.

    Raises:
        CapabilityNotSupportedError: If profile doesn't allow the capability.
    """

    async def dependency(
        sandbox_id: str,
        sandbox_mgr: SandboxManagerDep,
        owner: AuthDep,
    ) -> Sandbox:
        sandbox = await sandbox_mgr.get(sandbox_id, owner)
        settings = get_settings()
        profile = settings.get_profile(sandbox.profile_id)

        if profile is None:
            raise CapabilityNotSupportedError(
                message=f"Profile not found: {sandbox.profile_id}",
                capability=capability,
            )

        # Phase 2: multi-container profiles may not set legacy `profile.capabilities`.
        # In that case we derive capability set from container specs.
        available_caps = (
            list(profile.capabilities)
            if getattr(profile, "capabilities", None)
            else sorted(profile.get_all_capabilities())
        )

        if capability not in available_caps:
            raise CapabilityNotSupportedError(
                message=f"Profile '{sandbox.profile_id}' does not support capability: {capability}",
                capability=capability,
                available=available_caps,
            )

        return sandbox

    return dependency


# Capability-specific sandbox dependencies
PythonCapabilityDep = Annotated[Sandbox, Depends(require_capability("python"))]
ShellCapabilityDep = Annotated[Sandbox, Depends(require_capability("shell"))]
FilesystemCapabilityDep = Annotated[Sandbox, Depends(require_capability("filesystem"))]
BrowserCapabilityDep = Annotated[Sandbox, Depends(require_capability("browser"))]
