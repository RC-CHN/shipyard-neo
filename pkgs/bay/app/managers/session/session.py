"""SessionManager - manages session (container) lifecycle.

Key responsibility: ensure_running - idempotent session startup.

See: plans/bay-design.md section 3.2
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import ProfileConfig, get_settings
from app.drivers.base import ContainerStatus, Driver
from app.errors import NotFoundError, SessionNotReadyError
from app.models.session import Session, SessionStatus
from app.models.cargo import Cargo
from app.services.http import http_client_manager

logger = structlog.get_logger()


class SessionManager:
    """Manages session (container) lifecycle."""

    def __init__(
        self,
        driver: Driver,
        db_session: AsyncSession,
    ) -> None:
        self._driver = driver
        self._db = db_session
        self._log = logger.bind(manager="session")
        self._settings = get_settings()

    async def create(
        self,
        sandbox_id: str,
        workspace: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Create a new session record (does not start container).
        
        Args:
            sandbox_id: Sandbox ID
            workspace: Cargo to mount
            profile: Profile configuration
            
        Returns:
            Created session
        """
        session_id = f"sess-{uuid.uuid4().hex[:12]}"

        self._log.info(
            "session.create",
            session_id=session_id,
            sandbox_id=sandbox_id,
            profile_id=profile.id,
        )

        session = Session(
            id=session_id,
            sandbox_id=sandbox_id,
            runtime_type=profile.runtime_type,
            profile_id=profile.id,
            desired_state=SessionStatus.PENDING,
            observed_state=SessionStatus.PENDING,
            created_at=datetime.utcnow(),
            last_active_at=datetime.utcnow(),
        )

        self._db.add(session)
        await self._db.commit()
        await self._db.refresh(session)

        return session

    async def get(self, session_id: str) -> Session | None:
        """Get session by ID."""
        result = await self._db.execute(select(Session).where(Session.id == session_id))
        return result.scalars().first()

    async def ensure_running(
        self,
        session: Session,
        workspace: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Ensure session is running - create/start container if needed.
        
        This is the core idempotent startup logic.
        
        Args:
            session: Session to ensure is running
            workspace: Cargo to mount
            profile: Profile configuration
            
        Returns:
            Updated session with endpoint
            
        Raises:
            SessionNotReadyError: If session is starting but not ready yet
        """
        self._log.info(
            "session.ensure_running",
            session_id=session.id,
            observed_state=session.observed_state,
        )

        # Already running and ready
        if session.is_ready:
            return session

        # Currently starting - tell client to retry
        if session.observed_state == SessionStatus.STARTING:
            raise SessionNotReadyError(
                message="Session is starting",
                sandbox_id=session.sandbox_id,
                retry_after_ms=1000,
            )

        # Need to create container
        if session.container_id is None:
            session.desired_state = SessionStatus.RUNNING
            session.observed_state = SessionStatus.STARTING
            await self._db.commit()

            # Create container
            container_id = await self._driver.create(
                session=session,
                profile=profile,
                workspace=workspace,
            )

            session.container_id = container_id
            await self._db.commit()

        # Need to start container
        if session.observed_state != SessionStatus.RUNNING:
            try:
                endpoint = await self._driver.start(
                    session.container_id,
                    runtime_port=int(profile.runtime_port or 8000),
                )
                session.endpoint = endpoint
                
                # Wait for Ship runtime to be ready before marking as RUNNING
                await self._wait_for_ready(endpoint, session.id)
                
                session.observed_state = SessionStatus.RUNNING
                session.last_observed_at = datetime.utcnow()
                await self._db.commit()

            except Exception as e:
                self._log.error(
                    "session.start_failed",
                    session_id=session.id,
                    error=str(e),
                )
                session.observed_state = SessionStatus.FAILED
                await self._db.commit()
                raise

        return session

    async def _wait_for_ready(
        self,
        endpoint: str,
        session_id: str,
        *,
        max_wait_seconds: float = 120.0,
        initial_interval: float = 0.5,
        max_interval: float = 1.0,
        backoff_factor: float = 2.0,
    ) -> None:
        """Wait for Ship runtime to be ready using exponential backoff.
        
        Polls the /health endpoint until it responds successfully.
        Uses generous timeouts to accommodate image pulling in production.
        Uses shared HTTP client for connection pooling efficiency.
        
        Args:
            endpoint: Ship endpoint URL
            session_id: Session ID for logging
            max_wait_seconds: Maximum total time to wait (default 120s for image pull)
            initial_interval: Initial retry interval in seconds
            max_interval: Maximum retry interval in seconds
            backoff_factor: Multiplier for exponential backoff
            
        Raises:
            SessionNotReadyError: If runtime doesn't become ready in time
        """
        url = f"{endpoint.rstrip('/')}/health"
        
        start_time = asyncio.get_event_loop().time()
        interval = initial_interval
        attempt = 0
        
        # Use shared HTTP client for connection pooling
        # Falls back to creating temporary client if not initialized
        try:
            client = http_client_manager.client
        except RuntimeError:
            # Fallback for tests or when lifespan not used
            client = None
        
        while True:
            attempt += 1
            try:
                if client is not None:
                    # Use shared client
                    response = await client.get(url, timeout=2.0)
                else:
                    # Fallback: create temporary client
                    async with httpx.AsyncClient() as temp_client:
                        response = await temp_client.get(url, timeout=2.0)
                
                if response.status_code == 200:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    self._log.info(
                        "session.runtime_ready",
                        session_id=session_id,
                        attempts=attempt,
                        elapsed_ms=int(elapsed * 1000),
                    )
                    return
            except (httpx.RequestError, httpx.TimeoutException):
                pass
            
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= max_wait_seconds:
                break
            
            # Exponential backoff with max cap
            await asyncio.sleep(min(interval, max_wait_seconds - elapsed))
            interval = min(interval * backoff_factor, max_interval)
        
        self._log.error(
            "session.runtime_not_ready",
            session_id=session_id,
            endpoint=endpoint,
            attempts=attempt,
            elapsed_seconds=max_wait_seconds,
        )
        raise SessionNotReadyError(
            message="Runtime failed to become ready",
            sandbox_id=session_id,
            retry_after_ms=1000,
        )

    async def stop(self, session: Session) -> None:
        """Stop a session (reclaim compute).
        
        Args:
            session: Session to stop
        """
        self._log.info("session.stop", session_id=session.id)

        session.desired_state = SessionStatus.STOPPED
        session.observed_state = SessionStatus.STOPPING
        await self._db.commit()

        if session.container_id:
            await self._driver.stop(session.container_id)

        session.observed_state = SessionStatus.STOPPED
        session.endpoint = None
        session.last_observed_at = datetime.utcnow()
        await self._db.commit()

    async def destroy(self, session: Session) -> None:
        """Destroy a session completely.
        
        Args:
            session: Session to destroy
        """
        self._log.info("session.destroy", session_id=session.id)

        if session.container_id:
            await self._driver.destroy(session.container_id)

        await self._db.delete(session)
        await self._db.commit()

    async def refresh_status(self, session: Session) -> Session:
        """Refresh session status from driver.
        
        Args:
            session: Session to refresh
            
        Returns:
            Updated session
        """
        if not session.container_id:
            return session

        info = await self._driver.status(
            session.container_id,
            runtime_port=int(self._settings.get_profile(session.profile_id).runtime_port or 8000)
            if self._settings.get_profile(session.profile_id)
            else None,
        )

        # Map container status to session status
        if info.status == ContainerStatus.RUNNING:
            session.observed_state = SessionStatus.RUNNING
            session.endpoint = info.endpoint
        elif info.status == ContainerStatus.CREATED:
            session.observed_state = SessionStatus.PENDING
        elif info.status == ContainerStatus.EXITED:
            session.observed_state = SessionStatus.STOPPED
        elif info.status == ContainerStatus.NOT_FOUND:
            session.observed_state = SessionStatus.STOPPED
            session.container_id = None

        session.last_observed_at = datetime.utcnow()
        await self._db.commit()

        return session

    async def touch(self, session_id: str) -> None:
        """Update last_active_at timestamp."""
        result = await self._db.execute(select(Session).where(Session.id == session_id))
        session = result.scalars().first()

        if session:
            session.last_active_at = datetime.utcnow()
            await self._db.commit()
