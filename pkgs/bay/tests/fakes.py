"""Fake implementations for testing.

These fakes allow unit tests to run without real Docker/infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.drivers.base import (
    ContainerInfo,
    ContainerStatus,
    Driver,
    MultiContainerInfo,
    RuntimeInstance,
)

if TYPE_CHECKING:
    from app.config import ProfileConfig
    from app.models.cargo import Cargo
    from app.models.session import Session


@dataclass
class FakeContainerState:
    """State of a fake container."""

    container_id: str
    session_id: str
    profile_id: str
    cargo_id: str
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

    Phase 1.5 additions:
    - status_calls: list of (container_id, runtime_port) for tracking probe calls
    - status_override: optional callback to customize status() behavior
    - status_exception: optional exception to raise on status() calls
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

        # Phase 1.5: status() tracking and customization
        self.status_calls: list[dict[str, Any]] = []
        self._status_override: dict[str, ContainerInfo] | None = None
        self._status_exception: Exception | None = None

    def set_status_override(self, container_id: str, info: ContainerInfo) -> None:
        """Set a custom status response for a specific container.

        Use this to simulate dead containers (EXITED/NOT_FOUND).
        """
        if self._status_override is None:
            self._status_override = {}
        self._status_override[container_id] = info

    def clear_status_override(self, container_id: str | None = None) -> None:
        """Clear status override(s)."""
        if container_id is None:
            self._status_override = None
        elif self._status_override is not None:
            self._status_override.pop(container_id, None)

    def set_status_exception(self, exception: Exception | None) -> None:
        """Set an exception to raise on all status() calls.

        Use this to simulate Docker daemon unreachable.
        """
        self._status_exception = exception

    async def create(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
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
            cargo_id=cargo.id,
            status=ContainerStatus.CREATED,
        )

        self.create_calls.append({
            "session_id": session.id,
            "profile_id": profile.id,
            "cargo_id": cargo.id,
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
        """Get fake container status.

        Phase 1.5: Supports override and exception injection for testing probes.
        """
        # Track the call for assertions
        self.status_calls.append({
            "container_id": container_id,
            "runtime_port": runtime_port,
        })

        # Check for exception injection (simulate Docker daemon unreachable)
        if self._status_exception is not None:
            raise self._status_exception

        # Check for override (simulate dead container)
        if self._status_override is not None and container_id in self._status_override:
            return self._status_override[container_id]

        # Default behavior: return actual fake container state
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
                        "bay.cargo_id": state.cargo_id,
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

    # Phase 2: Multi-container support

    def __init_multi(self) -> None:
        """Lazy init for multi-container tracking fields."""
        if not hasattr(self, "_networks"):
            self._networks: dict[str, str] = {}  # session_id -> network_name
            self.create_network_calls: list[str] = []
            self.remove_network_calls: list[str] = []
            self.create_multi_calls: list[dict[str, Any]] = []
            self.start_multi_calls: list[list[str]] = []
            self.stop_multi_calls: list[list[str]] = []
            self.destroy_multi_calls: list[list[str]] = []
            # Error injection for multi-container testing
            self._create_multi_fail_on: str | None = None  # container name to fail on

    def set_create_multi_fail_on(self, container_name: str | None) -> None:
        """Set a container name that should fail during create_multi."""
        self.__init_multi()
        self._create_multi_fail_on = container_name

    async def create_session_network(self, session_id: str) -> str:
        """Create a fake session network."""
        self.__init_multi()
        network_name = f"bay_net_{session_id}"
        self._networks[session_id] = network_name
        self.create_network_calls.append(session_id)
        return network_name

    async def remove_session_network(self, session_id: str) -> None:
        """Remove a fake session network."""
        self.__init_multi()
        self.remove_network_calls.append(session_id)
        self._networks.pop(session_id, None)

    async def create_multi(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
        *,
        network_name: str,
        labels: dict[str, str] | None = None,
    ) -> list[MultiContainerInfo]:
        """Create multiple fake containers."""
        self.__init_multi()

        self.create_multi_calls.append({
            "session_id": session.id,
            "profile_id": profile.id,
            "cargo_id": cargo.id,
            "network_name": network_name,
        })

        results: list[MultiContainerInfo] = []
        for spec in profile.get_containers():
            # Check if this container should fail
            if self._create_multi_fail_on and spec.name == self._create_multi_fail_on:
                # Rollback already-created
                for created in results:
                    if created.container_id in self._containers:
                        del self._containers[created.container_id]
                raise RuntimeError(
                    f"Fake: create_multi failed on container '{spec.name}'"
                )

            container_id = f"fake-container-{self._next_container_id}"
            self._next_container_id += 1

            self._containers[container_id] = FakeContainerState(
                container_id=container_id,
                session_id=session.id,
                profile_id=profile.id,
                cargo_id=cargo.id,
                status=ContainerStatus.CREATED,
            )

            results.append(
                MultiContainerInfo(
                    name=spec.name,
                    container_id=container_id,
                    runtime_type=spec.runtime_type,
                    capabilities=list(spec.capabilities),
                    status=ContainerStatus.CREATED,
                )
            )

        return results

    async def start_multi(
        self,
        containers: list[MultiContainerInfo],
    ) -> list[MultiContainerInfo]:
        """Start multiple fake containers."""
        self.__init_multi()
        self.start_multi_calls.append([c.name for c in containers])

        for c in containers:
            if c.container_id in self._containers:
                state = self._containers[c.container_id]
                runtime_port = 8123  # default
                state.status = ContainerStatus.RUNNING
                state.endpoint = f"http://fake-{c.name}:{runtime_port}"
                c.endpoint = state.endpoint
                c.status = ContainerStatus.RUNNING

        return containers

    async def stop_multi(self, containers: list[MultiContainerInfo]) -> None:
        """Stop multiple fake containers."""
        self.__init_multi()
        self.stop_multi_calls.append([c.name for c in containers])

        for c in containers:
            if c.container_id in self._containers:
                self._containers[c.container_id].status = ContainerStatus.EXITED
                self._containers[c.container_id].endpoint = None

    async def destroy_multi(self, containers: list[MultiContainerInfo]) -> None:
        """Destroy multiple fake containers."""
        self.__init_multi()
        self.destroy_multi_calls.append([c.name for c in containers])

        for c in containers:
            if c.container_id in self._containers:
                del self._containers[c.container_id]
