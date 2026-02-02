"""Fake implementations for testing.

These fakes allow unit tests to run without real Docker/infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.drivers.base import ContainerInfo, ContainerStatus, Driver, RuntimeInstance

if TYPE_CHECKING:
    from app.config import ProfileConfig
    from app.models.session import Session
    from app.models.workspace import Workspace


@dataclass
class FakeContainerState:
    """State of a fake container."""

    container_id: str
    session_id: str
    profile_id: str
    workspace_id: str
    status: ContainerStatus = ContainerStatus.CREATED
    endpoint: str | None = None


@dataclass
class FakeVolumeState:
    """State of a fake volume."""

    name: str
    labels: dict[str, str] = field(default_factory=dict)


class FakeDriver(Driver):
    """Fake driver for unit testing.
    
    Records all method calls for assertion and provides controlled responses.
    """

    def __init__(self) -> None:
        self._containers: dict[str, FakeContainerState] = {}
        self._volumes: dict[str, FakeVolumeState] = {}
        self._next_container_id = 1
        
        # Call counters for assertions
        self.create_calls: list[dict[str, Any]] = []
        self.start_calls: list[dict[str, Any]] = []
        self.stop_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.create_volume_calls: list[dict[str, Any]] = []
        self.delete_volume_calls: list[str] = []

    async def create(
        self,
        session: "Session",
        profile: "ProfileConfig",
        workspace: "Workspace",
        *,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a fake container."""
        container_id = f"fake-container-{self._next_container_id}"
        self._next_container_id += 1
        
        self._containers[container_id] = FakeContainerState(
            container_id=container_id,
            session_id=session.id,
            profile_id=profile.id,
            workspace_id=workspace.id,
            status=ContainerStatus.CREATED,
        )
        
        self.create_calls.append({
            "session_id": session.id,
            "profile_id": profile.id,
            "workspace_id": workspace.id,
            "labels": labels,
        })
        
        return container_id

    async def start(self, container_id: str, *, runtime_port: int) -> str:
        """Start a fake container and return endpoint."""
        if container_id not in self._containers:
            raise ValueError(f"Container not found: {container_id}")
        
        container = self._containers[container_id]
        container.status = ContainerStatus.RUNNING
        container.endpoint = f"http://fake-host:{runtime_port}"
        
        self.start_calls.append({
            "container_id": container_id,
            "runtime_port": runtime_port,
        })
        
        return container.endpoint

    async def stop(self, container_id: str) -> None:
        """Stop a fake container."""
        self.stop_calls.append(container_id)
        
        if container_id in self._containers:
            self._containers[container_id].status = ContainerStatus.EXITED
            self._containers[container_id].endpoint = None

    async def destroy(self, container_id: str) -> None:
        """Destroy a fake container."""
        self.destroy_calls.append(container_id)
        
        if container_id in self._containers:
            del self._containers[container_id]

    async def status(self, container_id: str, *, runtime_port: int | None = None) -> ContainerInfo:
        """Get fake container status."""
        if container_id not in self._containers:
            return ContainerInfo(
                container_id=container_id,
                status=ContainerStatus.NOT_FOUND,
            )
        
        container = self._containers[container_id]
        return ContainerInfo(
            container_id=container_id,
            status=container.status,
            endpoint=container.endpoint,
        )

    async def logs(self, container_id: str, tail: int = 100) -> str:
        """Get fake container logs."""
        return f"Fake logs for {container_id}"

    async def create_volume(self, name: str, labels: dict[str, str] | None = None) -> str:
        """Create a fake volume."""
        self._volumes[name] = FakeVolumeState(name=name, labels=labels or {})
        
        self.create_volume_calls.append({
            "name": name,
            "labels": labels,
        })
        
        return name

    async def delete_volume(self, name: str) -> None:
        """Delete a fake volume."""
        self.delete_volume_calls.append(name)
        
        if name in self._volumes:
            del self._volumes[name]

    async def volume_exists(self, name: str) -> bool:
        """Check if fake volume exists."""
        return name in self._volumes

    # GC-related methods

    async def list_runtime_instances(
        self, *, labels: dict[str, str]
    ) -> list[RuntimeInstance]:
        """List fake runtime instances matching labels."""
        instances = []
        for container_id, state in self._containers.items():
            # For testing, we'll return all containers
            # In a real implementation, we'd filter by labels
            instances.append(
                RuntimeInstance(
                    id=container_id,
                    name=f"bay-session-{state.session_id}",
                    labels={
                        "bay.session_id": state.session_id,
                        "bay.workspace_id": state.workspace_id,
                        "bay.profile_id": state.profile_id,
                        "bay.managed": "true",
                        "bay.instance_id": "bay",
                    },
                    state=state.status.value,
                )
            )
        return instances

    async def destroy_runtime_instance(self, instance_id: str) -> None:
        """Force destroy a fake runtime instance."""
        if instance_id in self._containers:
            del self._containers[instance_id]
