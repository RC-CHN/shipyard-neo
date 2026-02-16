"""Sandbox data model.

Sandbox is the only external-facing resource.
- Stable ID that clients hold onto
- Aggregates Cargo + Profile + Session(s)
- Session can be recycled/recreated transparently
"""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional

from sqlmodel import Field, Relationship, SQLModel

from app.utils.datetime import utcnow

if TYPE_CHECKING:
    from app.models.cargo import Cargo
    from app.models.session import Session


class SandboxStatus(str, Enum):
    """Aggregated sandbox status (for external API)."""

    IDLE = "idle"  # No running session
    STARTING = "starting"  # Session is starting
    READY = "ready"  # Session is running and ready
    FAILED = "failed"  # Last session start failed
    EXPIRED = "expired"  # TTL expired
    DELETED = "deleted"  # Soft-deleted (internal only)


class Sandbox(SQLModel, table=True):
    """Sandbox - external-facing resource."""

    __tablename__ = "sandboxes"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    # Profile (runtime specification)
    profile_id: str = Field(default="python-default")

    # Cargo relationship
    # Note: cargo_id can become NULL after sandbox is soft-deleted and
    # its managed cargo is cascade-deleted. For active sandboxes (deleted_at IS NULL),
    # cargo_id is guaranteed to be set at creation time.
    cargo_id: Optional[str] = Field(default=None, foreign_key="cargos.id", index=True)

    # Current session (single session for Phase 1)
    current_session_id: Optional[str] = Field(default=None, index=True)

    # TTL management
    expires_at: Optional[datetime] = Field(default=None)  # null = no expiry
    idle_expires_at: Optional[datetime] = Field(default=None)

    # Soft delete (tombstone)
    deleted_at: Optional[datetime] = Field(default=None, index=True)

    # Optimistic locking
    version: int = Field(default=1)

    # Timestamps
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)

    # Relationships
    cargo: "Cargo" = Relationship(back_populates="sandboxes")
    sessions: list["Session"] = Relationship(back_populates="sandbox")

    @property
    def is_deleted(self) -> bool:
        """Check if sandbox is soft-deleted."""
        return self.deleted_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if sandbox TTL has expired."""
        if self.expires_at is None:
            return False
        return utcnow() > self.expires_at

    def compute_status(
        self,
        *,
        now: datetime,
        current_session: "Optional[Session]" = None,
    ) -> SandboxStatus:
        """Compute aggregated status for external API.

        Args:
            now: Fixed time reference for deterministic computation
            current_session: The current session object (if loaded)
        """
        from app.models.session import SessionStatus

        if self.deleted_at is not None:
            return SandboxStatus.DELETED
        if self.expires_at is not None and now > self.expires_at:
            return SandboxStatus.EXPIRED

        if current_session is None:
            return SandboxStatus.IDLE

        if current_session.observed_state == SessionStatus.RUNNING:
            return SandboxStatus.READY
        if current_session.observed_state in (SessionStatus.PENDING, SessionStatus.STARTING):
            return SandboxStatus.STARTING
        if current_session.observed_state == SessionStatus.FAILED:
            return SandboxStatus.FAILED

        return SandboxStatus.IDLE
