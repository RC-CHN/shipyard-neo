"""Cargo data model.

Cargo represents a persistent data volume that can be shared across Sessions.

Types:
- managed: Created implicitly by POST /sandboxes, cascade-deleted with Sandbox
- external: Created explicitly by POST /cargos, never cascade-deleted
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.datetime import utcnow

if TYPE_CHECKING:
    from app.models.sandbox import Sandbox


class Cargo(SQLModel, table=True):
    """Cargo - persistent data storage."""

    __tablename__ = "cargos"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    # Storage backend
    backend: str = Field(default="docker_volume")  # docker_volume | k8s_pvc
    driver_ref: str = Field(default="")  # Volume name or PVC name

    # Managed workspace relationship
    managed: bool = Field(default=True)
    managed_by_sandbox_id: Optional[str] = Field(default=None, index=True)

    # Quota
    size_limit_mb: int = Field(default=1024)

    # Timestamps
    created_at: datetime = Field(default_factory=utcnow)
    last_accessed_at: datetime = Field(default_factory=utcnow)

    # Relationships
    sandboxes: list["Sandbox"] = Relationship(back_populates="cargo")

    # Fixed mount path (not stored, constant)
    @property
    def mount_path(self) -> str:
        """Container mount path - always /workspace."""
        return "/workspace"
