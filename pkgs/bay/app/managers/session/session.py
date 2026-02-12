"""SessionManager - manages session (container) lifecycle.

Key responsibility: ensure_running - idempotent session startup.

Phase 2: Multi-container support.
- Single-container profiles (Phase 1 path) use create/start for backward compatibility.
- Multi-container profiles use create_multi/start_multi for parallel container orchestration.
- Session.containers JSON field tracks per-container state (name, container_id, endpoint, status).

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
from app.drivers.base import ContainerStatus, Driver, MultiContainerInfo
from app.errors import SessionNotReadyError
from app.models.cargo import Cargo
from app.models.session import Session, SessionStatus
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
        cargo: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Create a new session record (does not start container).

        Args:
            sandbox_id: Sandbox ID
            cargo: Cargo to mount
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

        primary = profile.get_primary_container()
        runtime_type = primary.runtime_type if primary else "ship"

        session = Session(
            id=session_id,
            sandbox_id=sandbox_id,
            runtime_type=runtime_type,
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
        cargo: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Ensure session is running - create/start container if needed.

        This is the core idempotent startup logic.

        Phase 2: Routes to multi-container path if profile has >1 container.
        Phase 1.5: Adds proactive health probing to detect dead containers
        before they cause runtime errors.

        Args:
            session: Session to ensure is running
            cargo: Cargo to mount
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
            container_count=len(profile.get_containers()),
        )

        # Phase 2: Multi-container path
        if len(profile.get_containers()) > 1:
            return await self._ensure_running_multi(session, cargo, profile)

        # Phase 1 path: single container (backward compatible)
        return await self._ensure_running_single(session, cargo, profile)

    async def _ensure_running_single(
        self,
        session: Session,
        cargo: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Single-container ensure_running (Phase 1 path).

        Backward compatible with existing single-container profiles.
        """
        # Phase 1.5: Proactive health probing
        # If DB says RUNNING but container might be dead, probe before trusting
        if session.container_id is not None and session.observed_state == SessionStatus.RUNNING:
            session = await self._probe_and_recover_if_dead(session, cargo, profile)

        # Already running and ready (after probe)
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

            try:
                # Create container
                container_id = await self._driver.create(
                    session=session,
                    profile=profile,
                    cargo=cargo,
                )
            except Exception as e:
                self._log.error(
                    "session.create_failed",
                    session_id=session.id,
                    error=str(e),
                )
                session.observed_state = SessionStatus.FAILED
                session.last_observed_at = datetime.utcnow()
                await self._db.commit()
                raise

            session.container_id = container_id
            await self._db.commit()

        # Need to start container
        if session.observed_state != SessionStatus.RUNNING:
            container_id = session.container_id
            primary = profile.get_primary_container()
            runtime_port = primary.runtime_port if primary else 8000
            try:
                endpoint = await self._driver.start(
                    container_id,
                    runtime_port=runtime_port,
                )

                # Wait for runtime to be ready before marking as RUNNING.
                # For browser containers (Gull), also checks that browser_ready=true
                # in the /health response. For ship containers, just checks HTTP 200.
                await self._wait_for_ready(
                    endpoint,
                    session_id=session.id,
                    sandbox_id=session.sandbox_id,
                    runtime_type=session.runtime_type,
                )

                # Only persist endpoint after readiness succeeds.
                session.endpoint = endpoint
                session.observed_state = SessionStatus.RUNNING
                session.last_observed_at = datetime.utcnow()
                await self._db.commit()

            except Exception as e:
                self._log.error(
                    "session.start_failed",
                    session_id=session.id,
                    container_id=container_id,
                    error=str(e),
                )
                # Best-effort cleanup: destroy any created container and clear runtime fields.
                if container_id is not None:
                    try:
                        await self._driver.destroy(container_id)
                    except Exception as destroy_error:
                        self._log.warning(
                            "session.destroy_failed",
                            session_id=session.id,
                            container_id=container_id,
                            error=str(destroy_error),
                        )
                session.container_id = None
                session.endpoint = None
                session.observed_state = SessionStatus.FAILED
                session.last_observed_at = datetime.utcnow()
                await self._db.commit()
                raise

        return session

    async def _ensure_running_multi(
        self,
        session: Session,
        cargo: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Multi-container ensure_running (Phase 2 path).

        Creates a session-scoped network, then creates and starts all
        containers in parallel. If any container fails, all are rolled back.

        The session's `containers` JSON field tracks per-container state.
        The legacy `container_id` and `endpoint` fields point to the primary container.
        """
        # Already running and ready
        if session.is_ready:
            return session

        # Currently starting - tell client to retry
        if session.observed_state == SessionStatus.STARTING:
            raise SessionNotReadyError(
                message="Session is starting (multi-container)",
                sandbox_id=session.sandbox_id,
                retry_after_ms=1500,
            )

        # Need to create and start containers
        if session.container_id is None:
            session.desired_state = SessionStatus.RUNNING
            session.observed_state = SessionStatus.STARTING
            await self._db.commit()

            network_name: str | None = None
            container_infos: list[MultiContainerInfo] = []

            try:
                # 1. Create session network
                network_name = await self._driver.create_session_network(session.id)

                # 2. Create all containers (on the session network)
                container_infos = await self._driver.create_multi(
                    session=session,
                    profile=profile,
                    cargo=cargo,
                    network_name=network_name,
                )

                # 3. Start all containers in parallel
                container_infos = await self._driver.start_multi(container_infos)

                # 4. Wait for all containers to be ready (health check)
                await self._wait_for_multi_ready(
                    container_infos,
                    session_id=session.id,
                    sandbox_id=session.sandbox_id,
                )

                # 5. Resolve primary container for backward compatibility
                primary_spec = profile.get_primary_container()
                primary_name = primary_spec.name if primary_spec else container_infos[0].name

                primary_info: MultiContainerInfo | None = None
                for ci in container_infos:
                    if ci.name == primary_name:
                        primary_info = ci
                        break

                if primary_info is None:
                    primary_info = container_infos[0]

                # 6. Persist to session
                session.container_id = primary_info.container_id
                session.endpoint = primary_info.endpoint
                session.containers = [ci.to_dict() for ci in container_infos]
                session.observed_state = SessionStatus.RUNNING
                session.last_observed_at = datetime.utcnow()
                await self._db.commit()

                self._log.info(
                    "session.multi_container_started",
                    session_id=session.id,
                    containers=[ci.name for ci in container_infos],
                    primary=primary_info.name,
                )

            except Exception as e:
                self._log.error(
                    "session.multi_container_failed",
                    session_id=session.id,
                    error=str(e),
                )

                # Rollback: destroy all containers + network
                if container_infos:
                    try:
                        await self._driver.destroy_multi(container_infos)
                    except Exception as cleanup_err:
                        self._log.warning(
                            "session.multi_container_rollback.destroy_failed",
                            error=str(cleanup_err),
                        )

                if network_name:
                    try:
                        await self._driver.remove_session_network(session.id)
                    except Exception as cleanup_err:
                        self._log.warning(
                            "session.multi_container_rollback.network_failed",
                            error=str(cleanup_err),
                        )

                session.container_id = None
                session.endpoint = None
                session.containers = None
                session.observed_state = SessionStatus.FAILED
                session.last_observed_at = datetime.utcnow()
                await self._db.commit()
                raise

        return session

    async def _wait_for_multi_ready(
        self,
        container_infos: list[MultiContainerInfo],
        *,
        session_id: str,
        sandbox_id: str,
        max_wait_seconds: float = 120.0,
        initial_interval: float = 0.5,
        max_interval: float = 1.0,
        backoff_factor: float = 2.0,
    ) -> None:
        """Wait for all containers in a multi-container session to be ready.

        Polls each container's /health endpoint until all respond successfully.
        For browser containers (Gull), also checks that browser_ready=true in
        the health response to ensure Chromium has been pre-warmed.
        """
        pending = {ci.name: ci for ci in container_infos}

        start_time = asyncio.get_event_loop().time()
        interval = initial_interval
        attempt = 0

        try:
            client = http_client_manager.client
        except RuntimeError:
            client = None

        while pending:
            attempt += 1
            newly_ready: list[str] = []

            for name, ci in pending.items():
                if ci.endpoint is None:
                    continue

                url = f"{ci.endpoint.rstrip('/')}/health"
                try:
                    if client is not None:
                        response = await client.get(url, timeout=2.0)
                    else:
                        async with httpx.AsyncClient(trust_env=False) as temp_client:
                            response = await temp_client.get(url, timeout=2.0)

                    if response.status_code == 200:
                        # For browser containers, also require browser_ready=true.
                        # This ensures Chromium is pre-warmed before marking ready.
                        # Old Gull images without browser_ready field are treated
                        # as ready (backward compat: field absence = ready).
                        if ci.runtime_type == "browser":
                            try:
                                payload = response.json()
                                browser_ready = payload.get("browser_ready", True)
                                if browser_ready:
                                    newly_ready.append(name)
                            except Exception:
                                # Can't parse JSON; treat as ready
                                newly_ready.append(name)
                        else:
                            newly_ready.append(name)
                except (httpx.RequestError, httpx.TimeoutException):
                    pass

            for name in newly_ready:
                del pending[name]

            if not pending:
                elapsed = asyncio.get_event_loop().time() - start_time
                self._log.info(
                    "session.multi_container_all_ready",
                    session_id=session_id,
                    attempts=attempt,
                    elapsed_ms=int(elapsed * 1000),
                )
                return

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= max_wait_seconds:
                break

            await asyncio.sleep(min(interval, max_wait_seconds - elapsed))
            interval = min(interval * backoff_factor, max_interval)

        self._log.error(
            "session.multi_container_not_ready",
            session_id=session_id,
            pending=list(pending.keys()),
            attempts=attempt,
        )
        raise SessionNotReadyError(
            message=f"Containers not ready: {list(pending.keys())}",
            sandbox_id=sandbox_id,
            retry_after_ms=1000,
        )

    async def _wait_for_ready(
        self,
        endpoint: str,
        *,
        session_id: str,
        sandbox_id: str,
        runtime_type: str = "ship",
        max_wait_seconds: float = 120.0,
        initial_interval: float = 0.5,
        max_interval: float = 1.0,
        backoff_factor: float = 2.0,
    ) -> None:
        """Wait for runtime to be ready using exponential backoff.

        Polls the /health endpoint until it responds with HTTP 200.
        - For browser containers (Gull): also checks that browser_ready=true
          in the health response to ensure Chromium has been pre-warmed.
          Old Gull images without browser_ready field are treated as ready
          (backward compat: field absence = ready).
        - For ship containers: just checks HTTP 200 (unchanged behavior).

        Uses generous timeouts to accommodate image pulling in production.
        Uses shared HTTP client for connection pooling efficiency.

        Args:
            endpoint: Runtime endpoint URL
            session_id: Session ID for logging
            sandbox_id: Sandbox ID for error metadata
            runtime_type: Container runtime type ("ship" or "browser")
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
                    async with httpx.AsyncClient(trust_env=False) as temp_client:
                        response = await temp_client.get(url, timeout=2.0)

                if response.status_code == 200:
                    # For browser containers, also require browser_ready=true.
                    # This ensures Chromium is pre-warmed before marking ready.
                    # Old Gull images without browser_ready field are treated
                    # as ready (backward compat: field absence = ready).
                    if runtime_type == "browser":
                        try:
                            payload = response.json()
                            browser_ready = payload.get("browser_ready", True)
                            if not browser_ready:
                                # /health returned 200 but browser not yet warm
                                pass  # fall through to retry
                            else:
                                elapsed = asyncio.get_event_loop().time() - start_time
                                self._log.info(
                                    "session.runtime_ready",
                                    session_id=session_id,
                                    attempts=attempt,
                                    elapsed_ms=int(elapsed * 1000),
                                )
                                return
                        except Exception:
                            # Can't parse JSON; treat as ready (backward compat)
                            elapsed = asyncio.get_event_loop().time() - start_time
                            self._log.info(
                                "session.runtime_ready",
                                session_id=session_id,
                                attempts=attempt,
                                elapsed_ms=int(elapsed * 1000),
                            )
                            return
                    else:
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
            sandbox_id=sandbox_id,
            retry_after_ms=1000,
        )

    async def stop(self, session: Session) -> None:
        """Stop a session (reclaim compute).

        Phase 2: If multi-container, stops all containers + removes network.

        Args:
            session: Session to stop
        """
        self._log.info(
            "session.stop",
            session_id=session.id,
            is_multi=session.is_multi_container,
        )

        session.desired_state = SessionStatus.STOPPED
        session.observed_state = SessionStatus.STOPPING
        await self._db.commit()

        if session.is_multi_container and session.containers:
            # Phase 2: Stop all containers
            container_infos = [
                MultiContainerInfo(
                    name=c["name"],
                    container_id=c["container_id"],
                    runtime_type=c.get("runtime_type", "ship"),
                    capabilities=c.get("capabilities", []),
                )
                for c in session.containers
            ]
            await self._driver.stop_multi(container_infos)
            # Remove session network
            try:
                await self._driver.remove_session_network(session.id)
            except Exception as e:
                self._log.warning(
                    "session.stop.network_remove_failed",
                    session_id=session.id,
                    error=str(e),
                )
        elif session.container_id:
            # Phase 1: Stop single container
            await self._driver.stop(session.container_id)

        session.observed_state = SessionStatus.STOPPED
        session.endpoint = None
        session.containers = None
        session.last_observed_at = datetime.utcnow()
        await self._db.commit()

    async def destroy(self, session: Session) -> None:
        """Destroy a session completely.

        Phase 2: If multi-container, destroys all containers + removes network.

        Args:
            session: Session to destroy
        """
        self._log.info(
            "session.destroy",
            session_id=session.id,
            is_multi=session.is_multi_container,
        )

        if session.is_multi_container and session.containers:
            # Phase 2: Destroy all containers
            container_infos = [
                MultiContainerInfo(
                    name=c["name"],
                    container_id=c["container_id"],
                    runtime_type=c.get("runtime_type", "ship"),
                    capabilities=c.get("capabilities", []),
                )
                for c in session.containers
            ]
            await self._driver.destroy_multi(container_infos)
            # Remove session network
            try:
                await self._driver.remove_session_network(session.id)
            except Exception as e:
                self._log.warning(
                    "session.destroy.network_remove_failed",
                    session_id=session.id,
                    error=str(e),
                )
        elif session.container_id:
            # Phase 1: Destroy single container
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

        profile = self._settings.get_profile(session.profile_id)
        runtime_port = None
        if profile:
            primary = profile.get_primary_container()
            runtime_port = primary.runtime_port if primary else None

        info = await self._driver.status(
            session.container_id,
            runtime_port=runtime_port,
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

    async def _probe_and_recover_if_dead(
        self,
        session: Session,
        cargo: Cargo,
        profile: ProfileConfig,
    ) -> Session:
        """Probe container health and recover if dead.

        Phase 1.5: Proactive health probing to detect dead containers.

        This method is called when DB says RUNNING but we need to verify
        the container is actually alive before trusting that state.

        Args:
            session: Session to probe
            cargo: Cargo for potential rebuild
            profile: Profile for potential rebuild

        Returns:
            Session (possibly with cleared container_id if dead)
        """
        container_id = session.container_id
        if container_id is None:
            return session

        primary = profile.get_primary_container()
        runtime_port = primary.runtime_port if primary else 8000

        try:
            info = await self._driver.status(
                container_id,
                runtime_port=runtime_port,
            )
        except Exception as e:
            # Docker daemon unreachable - degrade to old path (trust DB state)
            self._log.warning(
                "session.probe_failed",
                session_id=session.id,
                container_id=container_id,
                error=str(e),
            )
            return session

        # Container is healthy - nothing to do
        if info.status == ContainerStatus.RUNNING:
            return session

        # Container is dead (EXITED/NOT_FOUND) - need recovery
        self._log.warning(
            "session.container_dead_detected",
            session_id=session.id,
            container_id=container_id,
            container_status=info.status.value,
        )

        # Best-effort destroy (container may already be gone)
        try:
            await self._driver.destroy(container_id)
        except Exception as destroy_error:
            self._log.debug(
                "session.destroy_dead_container_failed",
                session_id=session.id,
                container_id=container_id,
                error=str(destroy_error),
            )

        # Clear runtime fields and reset to PENDING for rebuild
        session.container_id = None
        session.endpoint = None
        session.observed_state = SessionStatus.PENDING
        session.last_observed_at = datetime.utcnow()
        await self._db.commit()

        self._log.info(
            "session.recovered_from_dead_container",
            session_id=session.id,
            old_container_id=container_id,
        )

        return session
