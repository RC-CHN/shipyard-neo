"""Driver base class - infrastructure abstraction.

Driver is responsible ONLY for container lifecycle management.
It does NOT handle:
- Authentication
- Retry/circuit-breaker
- Audit logging
- Rate limiting
- Quota management

Endpoint 解析注意：
- Bay 可能运行在宿主机，也可能运行在容器内（挂载 docker.sock）。
- 因此 Driver 需要支持：容器网络直连（container IP + runtime_port）以及宿主机端口映射（host port）。

Phase 2: Multi-container support
- create_session_network / remove_session_network: Session-scoped Docker network
- create_multi / start_multi / stop_multi / destroy_multi: Multi-container orchestration

See: plans/bay-design.md section 3.1
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import ContainerSpec, ProfileConfig
    from app.models.cargo import Cargo
    from app.models.session import Session


class ContainerStatus(str, Enum):
    """Container status from driver's perspective."""

    CREATED = "created"
    RUNNING = "running"
    EXITED = "exited"
    REMOVING = "removing"
    NOT_FOUND = "not_found"


@dataclass
class ContainerInfo:
    """Container information from driver."""

    container_id: str
    status: ContainerStatus
    endpoint: str | None = None  # Ship REST API endpoint
    exit_code: int | None = None


@dataclass
class MultiContainerInfo:
    """Information about a single container within a multi-container session.

    Phase 2: Used by multi-container orchestration methods.
    """

    name: str  # Container name from ContainerSpec (e.g., "ship", "gull")
    container_id: str  # Docker container ID
    endpoint: str | None = None  # HTTP endpoint (e.g., "http://host:port")
    status: ContainerStatus = ContainerStatus.CREATED
    runtime_type: str = "ship"  # ship | gull | custom
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dict for Session.containers JSON storage."""
        return {
            "name": self.name,
            "container_id": self.container_id,
            "endpoint": self.endpoint,
            "status": self.status.value,
            "runtime_type": self.runtime_type,
            "capabilities": self.capabilities,
        }


@dataclass
class RuntimeInstance:
    """Runtime instance information for GC discovery.

    Used by OrphanContainerGC to discover and clean up orphan containers.
    """

    id: str  # Container ID / Pod name
    name: str  # Container name
    labels: dict[str, str]
    state: str  # "running", "exited", etc.
    created_at: str | None = None  # ISO format timestamp


class Driver(ABC):
    """Abstract driver interface for container lifecycle management.

    All resources created by driver MUST be labeled with:
    - owner
    - sandbox_id
    - session_id
    - cargo_id
    - profile_id

    Note:
    - `runtime_port` 是 runtime 容器内暴露 HTTP API 的端口（例如 Ship 默认 8000）。
      不应在 Driver 中硬编码，应该由 Profile/配置传入。
    """

    @abstractmethod
    async def create(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
        *,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a container without starting it.

        Args:
            session: Session model
            profile: Profile configuration
            cargo: Cargo to mount
            labels: Additional labels for the container

        Returns:
            Container ID
        """
        ...

    @abstractmethod
    async def start(self, container_id: str, *, runtime_port: int) -> str:
        """Start a container and return its endpoint.

        Args:
            container_id: Container ID from create()
            runtime_port: Runtime HTTP port inside the container

        Returns:
            Runtime base URL (e.g., http://<ip>:<port> or http://127.0.0.1:<host_port>)
        """
        ...

    @abstractmethod
    async def stop(self, container_id: str) -> None:
        """Stop a running container.

        Args:
            container_id: Container ID
        """
        ...

    @abstractmethod
    async def destroy(self, container_id: str) -> None:
        """Destroy (remove) a container.

        Args:
            container_id: Container ID
        """
        ...

    @abstractmethod
    async def status(self, container_id: str, *, runtime_port: int | None = None) -> ContainerInfo:
        """Get container status.

        Args:
            container_id: Container ID
            runtime_port: Optional runtime HTTP port inside the container.
                If provided, driver may compute `endpoint`.

        Returns:
            Container information
        """
        ...

    @abstractmethod
    async def logs(self, container_id: str, tail: int = 100) -> str:
        """Get container logs.

        Args:
            container_id: Container ID
            tail: Number of lines to return

        Returns:
            Log content
        """
        ...

    # Volume management (for Cargo)

    @abstractmethod
    async def create_volume(self, name: str, labels: dict[str, str] | None = None) -> str:
        """Create a volume for cargo.

        Args:
            name: Volume name
            labels: Volume labels

        Returns:
            Volume name (for reference)
        """
        ...

    @abstractmethod
    async def delete_volume(self, name: str) -> None:
        """Delete a volume.

        Args:
            name: Volume name
        """
        ...

    @abstractmethod
    async def volume_exists(self, name: str) -> bool:
        """Check if volume exists.

        Args:
            name: Volume name

        Returns:
            True if volume exists
        """
        ...

    # Runtime instance discovery (for GC)

    @abstractmethod
    async def list_runtime_instances(
        self, *, labels: dict[str, str]
    ) -> list[RuntimeInstance]:
        """List runtime instances matching labels.

        Used by OrphanContainerGC to discover containers that may be orphaned.
        Only returns instances that match ALL specified labels.

        Args:
            labels: Label filters (all must match)

        Returns:
            List of matching runtime instances
        """
        ...

    @abstractmethod
    async def destroy_runtime_instance(self, instance_id: str) -> None:
        """Force destroy a runtime instance.

        Used by OrphanContainerGC to clean up orphan containers.
        This is a low-level method that bypasses normal session cleanup.

        Args:
            instance_id: Instance ID (container ID / Pod name)
        """
        ...

    # Phase 2: Multi-container orchestration

    async def create_session_network(self, session_id: str) -> str:
        """Create a session-scoped network for multi-container communication.

        Args:
            session_id: Session ID (used to generate unique network name)

        Returns:
            Network name/ID
        """
        raise NotImplementedError("Multi-container not supported by this driver")

    async def remove_session_network(self, session_id: str) -> None:
        """Remove a session-scoped network.

        Best-effort: logs warning if network not found.

        Args:
            session_id: Session ID
        """
        raise NotImplementedError("Multi-container not supported by this driver")

    async def create_multi(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
        *,
        network_name: str,
        labels: dict[str, str] | None = None,
    ) -> list["MultiContainerInfo"]:
        """Create multiple containers for a session (without starting them).

        Phase 2: Creates one container per ContainerSpec in the profile,
        all attached to the session network and sharing the cargo volume.

        Args:
            session: Session model
            profile: Profile configuration (with multiple containers)
            cargo: Cargo to mount
            network_name: Session network name (from create_session_network)
            labels: Additional labels for all containers

        Returns:
            List of MultiContainerInfo (one per container)
        """
        raise NotImplementedError("Multi-container not supported by this driver")

    async def start_multi(
        self,
        containers: list["MultiContainerInfo"],
    ) -> list["MultiContainerInfo"]:
        """Start multiple containers and resolve their endpoints.

        Phase 2: Starts all containers in parallel, waits for all to be running.

        Args:
            containers: List of MultiContainerInfo from create_multi

        Returns:
            Updated list with endpoints resolved
        """
        raise NotImplementedError("Multi-container not supported by this driver")

    async def stop_multi(self, containers: list["MultiContainerInfo"]) -> None:
        """Stop multiple containers.

        Phase 2: Stops all containers, best-effort (logs warnings on failures).

        Args:
            containers: List of MultiContainerInfo to stop
        """
        raise NotImplementedError("Multi-container not supported by this driver")

    async def destroy_multi(self, containers: list["MultiContainerInfo"]) -> None:
        """Destroy (remove) multiple containers.

        Phase 2: Force-removes all containers, best-effort.

        Args:
            containers: List of MultiContainerInfo to destroy
        """
        raise NotImplementedError("Multi-container not supported by this driver")
