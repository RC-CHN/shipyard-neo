"""BayClient - main entry point for Bay SDK."""

from __future__ import annotations

import os
from types import TracebackType

from shipyard_neo._http import HTTPClient
from shipyard_neo.cargo import CargoManager
from shipyard_neo.sandbox import Sandbox
from shipyard_neo.skills import SkillManager
from shipyard_neo.types import SandboxInfo, SandboxList, SandboxStatus, ProfileList


class BayClient:
    """Main client for Bay API.

    The primary entry point for interacting with Bay sandboxes.
    Use as an async context manager to ensure proper cleanup.

    Example:
        async with BayClient(
            endpoint_url="http://localhost:8000",
            access_token="your-token"
        ) as client:
            sandbox = await client.create_sandbox(ttl=3600)
            result = await sandbox.python.exec("print('hello')")
    """

    def __init__(
        self,
        endpoint_url: str | None = None,
        access_token: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        """Initialize Bay client.

        Args:
            endpoint_url: Bay API base URL. Falls back to BAY_ENDPOINT env var.
            access_token: Bearer token for authentication. Falls back to BAY_TOKEN env var.
            timeout: Default request timeout in seconds. Falls back to BAY_TIMEOUT env var.
            max_retries: Maximum retry attempts. Falls back to BAY_MAX_RETRIES env var.

        Raises:
            ValueError: If endpoint_url or access_token not provided and not in env.
        """
        self._endpoint_url = endpoint_url or os.environ.get("BAY_ENDPOINT")
        self._access_token = access_token or os.environ.get("BAY_TOKEN")

        if not self._endpoint_url:
            raise ValueError("endpoint_url required (or set BAY_ENDPOINT env var)")
        if not self._access_token:
            raise ValueError("access_token required (or set BAY_TOKEN env var)")

        # Allow env var overrides
        timeout_str = os.environ.get("BAY_TIMEOUT")
        if timeout_str and timeout == 30.0:  # Only if default
            timeout = float(timeout_str)

        retries_str = os.environ.get("BAY_MAX_RETRIES")
        if retries_str and max_retries == 3:  # Only if default
            max_retries = int(retries_str)

        self._timeout = timeout
        self._max_retries = max_retries
        self._http: HTTPClient | None = None

        # Cargo manager (initialized lazily with HTTP client)
        self._cargos: CargoManager | None = None
        self._skills: SkillManager | None = None

    async def __aenter__(self) -> BayClient:
        """Enter async context, initializing HTTP client."""
        self._http = HTTPClient(
            base_url=self._endpoint_url,
            access_token=self._access_token,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )
        await self._http.__aenter__()
        self._cargos = CargoManager(self._http)
        self._skills = SkillManager(self._http)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context, closing HTTP client."""
        if self._http:
            await self._http.__aexit__(exc_type, exc_val, exc_tb)
            self._http = None
            self._cargos = None
            self._skills = None

    @property
    def http(self) -> HTTPClient:
        """Get the HTTP client."""
        if self._http is None:
            raise RuntimeError("BayClient not initialized. Use 'async with' context.")
        return self._http

    @property
    def cargos(self) -> CargoManager:
        """Cargo management API."""
        if self._cargos is None:
            raise RuntimeError("BayClient not initialized. Use 'async with' context.")
        return self._cargos

    @property
    def skills(self) -> SkillManager:
        """Skill lifecycle API."""
        if self._skills is None:
            raise RuntimeError("BayClient not initialized. Use 'async with' context.")
        return self._skills

    # Sandbox operations

    async def create_sandbox(
        self,
        *,
        profile: str = "python-default",
        cargo_id: str | None = None,
        ttl: int | None = None,
        idempotency_key: str | None = None,
    ) -> Sandbox:
        """Create a new sandbox.

        Args:
            profile: Runtime profile (e.g., "python-default")
            cargo_id: Optional external cargo ID to attach
            ttl: Time-to-live in seconds. None or 0 = no expiry.
            idempotency_key: Optional key for safe retries

        Returns:
            Sandbox object for the created sandbox
        """
        body: dict = {"profile": profile}
        if cargo_id is not None:
            body["cargo_id"] = cargo_id
        if ttl is not None:
            body["ttl"] = ttl

        response = await self.http.post(
            "/v1/sandboxes",
            json=body,
            idempotency_key=idempotency_key,
        )
        info = SandboxInfo.model_validate(response)
        return Sandbox(self.http, info)

    async def get_sandbox(self, sandbox_id: str) -> Sandbox:
        """Get an existing sandbox.

        Args:
            sandbox_id: Sandbox ID

        Returns:
            Sandbox object

        Raises:
            NotFoundError: If sandbox doesn't exist
        """
        response = await self.http.get(f"/v1/sandboxes/{sandbox_id}")
        info = SandboxInfo.model_validate(response)
        return Sandbox(self.http, info)

    async def list_sandboxes(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: SandboxStatus | str | None = None,
    ) -> SandboxList:
        """List sandboxes.

        Args:
            limit: Max items per page (1-200)
            cursor: Pagination cursor from previous response
            status: Filter by status (SandboxStatus enum or string)

        Returns:
            SandboxList with items and next_cursor
        """
        # Convert enum to string if needed
        status_str = status.value if isinstance(status, SandboxStatus) else status

        response = await self.http.get(
            "/v1/sandboxes",
            params={
                "limit": limit,
                "cursor": cursor,
                "status": status_str,
            },
        )
        return SandboxList.model_validate(response)

    # Profile operations

    async def list_profiles(self, *, detail: bool = False) -> ProfileList:
        """List available sandbox profiles.

        Returns a list of runtime profiles with their resource specs,
        capabilities, and idle timeout configuration.

        Args:
            detail: If True, include per-container topology and description.

        Returns:
            ProfileList with available profiles
        """
        params: dict[str, str] = {}
        if detail:
            params["detail"] = "true"
        response = await self.http.get("/v1/profiles", params=params)
        return ProfileList.model_validate(response)
