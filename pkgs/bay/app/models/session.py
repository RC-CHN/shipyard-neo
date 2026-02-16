"""Session data model.

Session represents a running container group.

Phase 1: 1 Session = 1 Container
Phase 2: 1 Session = N Containers (multi-container support)

- Can be idle-recycled and recreated transparently
- Not exposed to external API (only sandbox_id is exposed)
"""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Column
from sqlmodel import Field, Relationship, SQLModel

from app.utils.datetime import utcnow

try:
    from sqlalchemy import JSON
except ImportError:
    from sqlalchemy.types import JSON

if TYPE_CHECKING:
    from app.models.sandbox import Sandbox


class SessionStatus(str, Enum):
    """Session lifecycle status."""

    PENDING = "pending"  # Waiting to be created
    STARTING = "starting"  # Container starting up
    RUNNING = "running"  # Running and healthy
    DEGRADED = "degraded"  # Partially running (some containers failed)
    STOPPING = "stopping"  # Stopping in progress
    STOPPED = "stopped"  # Stopped
    FAILED = "failed"  # Start failed


class ContainerRuntime(dict):
    """Runtime container state stored in Session.containers JSON.

    Schema: {
        "name": str,           # Container name (e.g., "ship", "browser")
        "container_id": str,   # Docker container ID
        "endpoint": str,       # HTTP endpoint (e.g., "http://host:port")
        "status": str,         # running | stopped | failed
        "runtime_type": str,   # ship | browser
        "capabilities": list,  # ["python", "shell"] etc.
    }
    """

    pass


class Session(SQLModel, table=True):
    """Session - running container group.

    Phase 2: Extended with `containers` JSON field for multi-container support.
    The legacy `container_id` and `endpoint` fields are kept for backward
    compatibility and always point to the primary container.
    """

    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    sandbox_id: str = Field(foreign_key="sandboxes.id", index=True)

    # Runtime info
    runtime_type: str = Field(default="ship")  # ship | browser | gpu (future)
    profile_id: str = Field(default="python-default")

    # Phase 1 compatible: Primary container info
    container_id: Optional[str] = Field(default=None)
    endpoint: Optional[str] = Field(default=None)  # Primary container REST API endpoint

    # Phase 2: Multi-container runtime state
    # Format: [{name, container_id, endpoint, status, runtime_type, capabilities}, ...]
    containers: Optional[list[dict[str, Any]]] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True, default=None),
    )

    # State management (desired vs observed)
    desired_state: SessionStatus = Field(default=SessionStatus.PENDING)
    observed_state: SessionStatus = Field(default=SessionStatus.PENDING)
    last_observed_at: Optional[datetime] = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)

    # Relationships
    sandbox: "Sandbox" = Relationship(back_populates="sessions")

    @property
    def is_ready(self) -> bool:
        """Check if session is ready to accept requests."""
        return self.observed_state == SessionStatus.RUNNING and self.endpoint is not None

    @property
    def is_running(self) -> bool:
        """Check if session is running (may not be ready yet)."""
        return self.observed_state in (SessionStatus.STARTING, SessionStatus.RUNNING)

    @property
    def is_multi_container(self) -> bool:
        """Check if this session uses multi-container mode."""
        return self.containers is not None and len(self.containers) > 1

    def get_container_for_capability(self, capability: str) -> dict[str, Any] | None:
        """Find a container that provides the given capability.

        Args:
            capability: The capability to look for (e.g., "python", "browser")

        Returns:
            Container dict if found, None otherwise
        """
        if not self.containers:
            return None

        for c in self.containers:
            if capability in c.get("capabilities", []):
                return c

        return None

    def get_container_endpoint(self, container_name: str) -> str | None:
        """Get endpoint for a specific container by name.

        Args:
            container_name: Container name (e.g., "ship", "browser")

        Returns:
            Endpoint URL if found, None otherwise
        """
        if not self.containers:
            return None

        for c in self.containers:
            if c.get("name") == container_name:
                return c.get("endpoint")

        return None
