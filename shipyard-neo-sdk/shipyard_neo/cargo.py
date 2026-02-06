"""Cargo manager for Bay SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shipyard_neo.types import CargoInfo, CargoList

if TYPE_CHECKING:
    from shipyard_neo._http import HTTPClient


class CargoManager:
    """Cargo management API.

    Cargos are persistent storage volumes that can be shared across sandboxes.
    - Managed cargo: Created automatically with sandbox, deleted with sandbox
    - External cargo: Created independently, must be explicitly deleted
    """

    def __init__(self, http: HTTPClient) -> None:
        """Initialize CargoManager.

        Args:
            http: HTTP client for making requests
        """
        self._http = http

    async def create(
        self,
        *,
        size_limit_mb: int | None = None,
        idempotency_key: str | None = None,
    ) -> CargoInfo:
        """Create an external cargo.

        Args:
            size_limit_mb: Size limit in MB (1-65536). If None, uses server default.
            idempotency_key: Optional key for safe retries

        Returns:
            CargoInfo for the created cargo
        """
        body: dict = {}
        if size_limit_mb is not None:
            body["size_limit_mb"] = size_limit_mb

        response = await self._http.post(
            "/v1/cargos",
            json=body if body else None,
            idempotency_key=idempotency_key,
        )
        return CargoInfo.model_validate(response)

    async def get(self, cargo_id: str) -> CargoInfo:
        """Get cargo details.

        Args:
            cargo_id: Cargo ID

        Returns:
            CargoInfo

        Raises:
            NotFoundError: If cargo doesn't exist
        """
        response = await self._http.get(f"/v1/cargos/{cargo_id}")
        return CargoInfo.model_validate(response)

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        managed: bool | None = None,
    ) -> CargoList:
        """List cargos.

        By default (managed=None), only external cargos (managed=false) are returned.
        Pass managed=True to see managed cargos instead.

        Args:
            limit: Max items per page (1-200)
            cursor: Pagination cursor from previous response
            managed: Filter by managed status. None = external only.

        Returns:
            CargoList with items and next_cursor
        """
        response = await self._http.get(
            "/v1/cargos",
            params={
                "limit": limit,
                "cursor": cursor,
                "managed": managed,
            },
        )
        return CargoList.model_validate(response)

    async def delete(self, cargo_id: str) -> None:
        """Delete a cargo.

        For external cargos:
        - Cannot delete if still referenced by active sandboxes
        - Returns 409 with active_sandbox_ids if in use

        For managed cargos:
        - Can delete if the managing sandbox is soft-deleted
        - Returns 409 if managing sandbox is still active

        Args:
            cargo_id: Cargo ID

        Raises:
            NotFoundError: If cargo doesn't exist
            ConflictError: If cargo is in use
        """
        await self._http.delete(f"/v1/cargos/{cargo_id}")
