"""Docker driver implementation using aiodocker.

Supports multiple connectivity modes between Bay and runtime containers:
- container_network: Bay reaches runtime by container IP on a docker network
- host_port: Bay reaches runtime via host port-mapping (127.0.0.1:<host_port>)
- auto: prefer container_network, fallback to host_port

This is necessary because Bay may run:
- on the host (typical): cannot directly reach container IP on a user-defined bridge
- inside a container (docker.sock mounted): can reach other containers via shared network

Phase 2: Multi-container orchestration
- Session-scoped networks (bay_net_{session_id})
- Parallel container creation/startup
- Shared Cargo Volume across all containers

Note: runtime_port is provided by ProfileConfig (do not hardcode Ship port here).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import aiodocker
import structlog
from aiodocker.exceptions import DockerError

from app.config import get_settings
from app.drivers.base import (
    ContainerInfo,
    ContainerStatus,
    Driver,
    MultiContainerInfo,
    RuntimeInstance,
)

if TYPE_CHECKING:
    from app.config import ContainerSpec, ProfileConfig
    from app.models.cargo import Cargo
    from app.models.session import Session

logger = structlog.get_logger()

# Cargo mount path inside container (fixed)
WORKSPACE_MOUNT_PATH = "/workspace"


def _parse_memory(memory_str: str) -> int:
    """Parse memory string (e.g., '1g', '512m') to bytes."""
    memory_str = memory_str.lower().strip()
    multipliers = {
        "k": 1024,
        "m": 1024 * 1024,
        "g": 1024 * 1024 * 1024,
    }
    if memory_str[-1] in multipliers:
        return int(float(memory_str[:-1]) * multipliers[memory_str[-1]])
    return int(memory_str)


class DockerDriver(Driver):
    """Docker driver implementation using aiodocker."""

    def __init__(self) -> None:
        settings = get_settings()
        # Parse socket URL
        socket_url = settings.driver.docker.socket
        if socket_url.startswith("unix://"):
            self._socket = socket_url
        else:
            self._socket = f"unix://{socket_url}"

        docker_cfg = settings.driver.docker
        self._network = docker_cfg.network
        self._connect_mode = docker_cfg.connect_mode
        self._host_address = docker_cfg.host_address
        self._publish_ports = docker_cfg.publish_ports
        self._host_port = docker_cfg.host_port

        self._log = logger.bind(driver="docker")
        self._client: aiodocker.Docker | None = None

    async def _get_client(self) -> aiodocker.Docker:
        """Get or create the aiodocker client."""
        if self._client is None:
            self._client = aiodocker.Docker(url=self._socket)
        return self._client

    async def close(self) -> None:
        """Close the docker client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _network_exists(self, name: str) -> bool:
        """Check if a docker network exists."""
        client = await self._get_client()
        try:
            await client.networks.get(name)
            return True
        except DockerError as e:
            if e.status == 404:
                return False
            raise

    def _resolve_container_ip(self, info: dict[str, Any]) -> str | None:
        networks = info.get("NetworkSettings", {}).get("Networks", {})
        if not networks:
            return None

        if self._network and self._network in networks:
            return networks[self._network].get("IPAddress")

        # fallback: first attached network
        return next(iter(networks.values())).get("IPAddress")

    def _resolve_host_port(
        self,
        info: dict[str, Any],
        *,
        runtime_port: int,
    ) -> tuple[str, int] | None:
        ports = info.get("NetworkSettings", {}).get("Ports", {})
        key = f"{runtime_port}/tcp"
        bindings = ports.get(key)
        if not bindings:
            return None

        # Docker returns list like [{"HostIp": "0.0.0.0", "HostPort": "32768"}]
        b0 = bindings[0]
        host_ip = (b0.get("HostIp") or "").strip()
        host_port_str = b0.get("HostPort")
        if not host_port_str:
            return None

        host_port = int(host_port_str)

        # If HostIp is 0.0.0.0, it means bound on all interfaces; use configured host address.
        if host_ip in ("", "0.0.0.0", "::"):
            host_ip = self._host_address

        return host_ip, host_port

    def _endpoint_from_hostport(self, host: str, port: int) -> str:
        return f"http://{host}:{port}"

    def _endpoint_from_container_ip(self, ip: str, runtime_port: int) -> str:
        return f"http://{ip}:{runtime_port}"

    async def create(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
        *,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a container without starting it.

        Phase 2: Uses primary container from profile for backward compatibility.
        For multi-container support, use create_multi() instead.
        """
        client = await self._get_client()

        # Phase 2: Get primary container spec
        primary = profile.get_primary_container()
        if primary is None:
            raise ValueError(f"Profile {profile.id} has no containers defined")

        runtime_port = primary.runtime_port
        image = primary.image

        # Get GC instance_id for container labeling
        settings = get_settings()
        gc_instance_id = settings.gc.get_instance_id()

        # Build labels (required for reconciliation and GC)
        container_labels = {
            "bay.owner": "default",  # TODO: get from session/sandbox
            "bay.sandbox_id": session.sandbox_id,
            "bay.session_id": session.id,
            "bay.cargo_id": cargo.id,
            "bay.profile_id": profile.id,
            "bay.runtime_port": str(runtime_port),
            # Labels for GC OrphanContainerGC Strict mode
            "bay.instance_id": gc_instance_id,
            "bay.managed": "true",
        }
        if labels:
            container_labels.update(labels)

        # Parse resource limits
        mem_limit = _parse_memory(primary.resources.memory)
        nano_cpus = int(primary.resources.cpus * 1e9)

        # Build environment
        env = [f"{k}={v}" for k, v in primary.env.items()]
        env.extend(
            [
                f"BAY_SESSION_ID={session.id}",
                f"BAY_SANDBOX_ID={session.sandbox_id}",
                f"BAY_WORKSPACE_PATH={WORKSPACE_MOUNT_PATH}",
            ]
        )

        self._log.info(
            "docker.create",
            session_id=session.id,
            image=image,
            cargo=cargo.driver_ref,
            runtime_port=runtime_port,
            connect_mode=self._connect_mode,
            network=self._network,
        )

        # Resolve network mode: if configured network doesn't exist, omit NetworkMode
        network_mode = None
        if self._network:
            if await self._network_exists(self._network):
                network_mode = self._network
            else:
                self._log.warning(
                    "docker.network_not_found.fallback_default",
                    network=self._network,
                )

        host_config: dict[str, Any] = {
            "Binds": [f"{cargo.driver_ref}:{WORKSPACE_MOUNT_PATH}:rw"],
            "Memory": mem_limit,
            "NanoCpus": nano_cpus,
            "PidsLimit": 256,
        }

        # Port publishing (needed for host_port mode, and for auto fallback)
        expose_key = f"{runtime_port}/tcp"
        exposed_ports: dict[str, dict[str, Any]] = {expose_key: {}}

        publish = bool(self._publish_ports) and self._connect_mode in ("host_port", "auto")
        port_bindings: dict[str, list[dict[str, str]]] | None = None
        if publish:
            host_port = self._host_port
            host_port_str = "" if (host_port is None or host_port == 0) else str(host_port)
            port_bindings = {
                expose_key: [
                    {
                        "HostIp": "0.0.0.0",
                        "HostPort": host_port_str,
                    }
                ]
            }
            host_config["PortBindings"] = port_bindings

        if network_mode and self._connect_mode in ("container_network", "auto"):
            host_config["NetworkMode"] = network_mode

        config: dict[str, Any] = {
            "Image": image,
            "Env": env,
            "Labels": container_labels,
            "HostConfig": host_config,
            "ExposedPorts": exposed_ports,
        }

        container = await client.containers.create(
            config=config,
            name=f"bay-session-{session.id}",
        )

        container_id = container.id
        self._log.info("docker.created", container_id=container_id)
        return container_id

    async def start(self, container_id: str, *, runtime_port: int) -> str:
        """Start container and return runtime endpoint."""
        client = await self._get_client()
        self._log.info(
            "docker.start",
            container_id=container_id,
            runtime_port=runtime_port,
            connect_mode=self._connect_mode,
        )

        container = client.containers.container(container_id)
        await container.start()

        info = await container.show()

        # 1) Prefer container network
        if self._connect_mode in ("container_network", "auto"):
            ip = self._resolve_container_ip(info)
            if ip:
                endpoint = self._endpoint_from_container_ip(ip, runtime_port)
                self._log.info("docker.endpoint.container_ip", endpoint=endpoint)
                return endpoint

        # 2) Fallback / host_port
        if self._connect_mode in ("host_port", "auto"):
            hp = self._resolve_host_port(info, runtime_port=runtime_port)
            if hp:
                host, port = hp
                endpoint = self._endpoint_from_hostport(host, port)
                self._log.info("docker.endpoint.host_port", endpoint=endpoint)
                return endpoint

        # 3) Last resort: container name (only works if Bay can resolve it)
        name = info.get("Name", "").lstrip("/")
        endpoint = f"http://{name}:{runtime_port}"
        self._log.warning("docker.endpoint.fallback_name", endpoint=endpoint)
        return endpoint

    async def stop(self, container_id: str) -> None:
        """Stop a running container."""
        client = await self._get_client()
        self._log.info("docker.stop", container_id=container_id)

        try:
            container = client.containers.container(container_id)
            await container.stop(timeout=10)
        except DockerError as e:
            if e.status == 404:
                self._log.warning("docker.stop.not_found", container_id=container_id)
            else:
                raise

    async def destroy(self, container_id: str) -> None:
        """Destroy (remove) a container."""
        client = await self._get_client()
        self._log.info("docker.destroy", container_id=container_id)

        try:
            container = client.containers.container(container_id)
            await container.delete(force=True)
        except DockerError as e:
            if e.status == 404:
                self._log.warning("docker.destroy.not_found", container_id=container_id)
            else:
                raise

    async def status(self, container_id: str, *, runtime_port: int | None = None) -> ContainerInfo:
        """Get container status."""
        client = await self._get_client()

        try:
            container = client.containers.container(container_id)
            info = await container.show()
        except DockerError as e:
            if e.status == 404:
                return ContainerInfo(
                    container_id=container_id,
                    status=ContainerStatus.NOT_FOUND,
                )
            raise

        docker_status = info.get("State", {}).get("Status", "unknown")

        if docker_status == "running":
            status = ContainerStatus.RUNNING
        elif docker_status == "created":
            status = ContainerStatus.CREATED
        elif docker_status in ("exited", "dead"):
            status = ContainerStatus.EXITED
        elif docker_status == "removing":
            status = ContainerStatus.REMOVING
        else:
            status = ContainerStatus.EXITED

        endpoint = None
        if status == ContainerStatus.RUNNING and runtime_port is not None:
            # container network first
            if self._connect_mode in ("container_network", "auto"):
                ip = self._resolve_container_ip(info)
                if ip:
                    endpoint = self._endpoint_from_container_ip(ip, runtime_port)

            # host port fallback
            if endpoint is None and self._connect_mode in ("host_port", "auto"):
                hp = self._resolve_host_port(info, runtime_port=runtime_port)
                if hp:
                    host, port = hp
                    endpoint = self._endpoint_from_hostport(host, port)

        # Get exit code
        exit_code = info.get("State", {}).get("ExitCode")

        return ContainerInfo(
            container_id=container_id,
            status=status,
            endpoint=endpoint,
            exit_code=exit_code,
        )

    async def logs(self, container_id: str, tail: int = 100) -> str:
        """Get container logs."""
        client = await self._get_client()

        try:
            container = client.containers.container(container_id)
            logs = await container.log(stdout=True, stderr=True, tail=tail)
            return "".join(logs)
        except DockerError as e:
            if e.status == 404:
                return ""
            raise

    # Volume management

    async def create_volume(self, name: str, labels: dict[str, str] | None = None) -> str:
        """Create a Docker volume."""
        client = await self._get_client()
        self._log.info("docker.create_volume", name=name)

        volume_labels = {"bay.managed": "true"}
        if labels:
            volume_labels.update(labels)

        volume = await client.volumes.create(
            {
                "Name": name,
                "Labels": volume_labels,
            }
        )

        # aiodocker returns DockerVolume object, get name from it
        return volume.name

    async def delete_volume(self, name: str) -> None:
        """Delete a Docker volume."""
        client = await self._get_client()
        self._log.info("docker.delete_volume", name=name)

        try:
            volume = await client.volumes.get(name)
            await volume.delete()
        except DockerError as e:
            if e.status == 404:
                self._log.warning("docker.delete_volume.not_found", name=name)
            else:
                raise

    async def volume_exists(self, name: str) -> bool:
        """Check if volume exists."""
        client = await self._get_client()

        try:
            await client.volumes.get(name)
            return True
        except DockerError as e:
            if e.status == 404:
                return False
            raise

    # Runtime instance discovery (for GC)

    async def list_runtime_instances(
        self, *, labels: dict[str, str]
    ) -> list[RuntimeInstance]:
        """List containers matching labels.

        Used by OrphanContainerGC to discover containers that may be orphaned.

        Args:
            labels: Label filters (all must match)

        Returns:
            List of matching runtime instances
        """
        client = await self._get_client()

        # Build label filter for Docker API
        # Format: label=key=value
        filters = {"label": [f"{k}={v}" for k, v in labels.items()]}

        self._log.debug(
            "docker.list_runtime_instances",
            filters=filters,
        )

        containers = await client.containers.list(all=True, filters=filters)

        instances = []
        for container in containers:
            # container is DockerContainer, need to get info
            info = await container.show()

            container_id = info.get("Id", "")
            name = info.get("Name", "").lstrip("/")
            container_labels = info.get("Config", {}).get("Labels", {})
            state = info.get("State", {}).get("Status", "unknown")
            created_at = info.get("Created")

            instances.append(
                RuntimeInstance(
                    id=container_id,
                    name=name,
                    labels=container_labels,
                    state=state,
                    created_at=created_at,
                )
            )

        self._log.debug(
            "docker.list_runtime_instances.result",
            count=len(instances),
        )

        return instances

    async def destroy_runtime_instance(self, instance_id: str) -> None:
        """Force destroy a container.

        Used by OrphanContainerGC to clean up orphan containers.

        Args:
            instance_id: Container ID
        """
        client = await self._get_client()
        self._log.info("docker.destroy_runtime_instance", instance_id=instance_id)

        try:
            container = client.containers.container(instance_id)
            await container.delete(force=True)
        except DockerError as e:
            if e.status == 404:
                self._log.warning(
                    "docker.destroy_runtime_instance.not_found",
                    instance_id=instance_id,
                )
            else:
                raise

    # ================================================================
    # Phase 2: Multi-container orchestration
    # ================================================================

    def _session_network_name(self, session_id: str) -> str:
        """Generate network name for a session."""
        return f"bay_net_{session_id}"

    async def create_session_network(self, session_id: str) -> str:
        """Create a session-scoped Docker bridge network.

        Args:
            session_id: Session ID

        Returns:
            Network name
        """
        client = await self._get_client()
        network_name = self._session_network_name(session_id)

        self._log.info(
            "docker.create_session_network",
            session_id=session_id,
            network_name=network_name,
        )

        settings = get_settings()
        gc_instance_id = settings.gc.get_instance_id()

        await client.networks.create(
            {
                "Name": network_name,
                "Driver": "bridge",
                "Labels": {
                    "bay.managed": "true",
                    "bay.session_id": session_id,
                    "bay.instance_id": gc_instance_id,
                },
            }
        )

        self._log.info("docker.session_network_created", network_name=network_name)
        return network_name

    async def remove_session_network(self, session_id: str) -> None:
        """Remove a session-scoped Docker network.

        Best-effort: logs warning if network not found.

        Args:
            session_id: Session ID
        """
        client = await self._get_client()
        network_name = self._session_network_name(session_id)

        self._log.info(
            "docker.remove_session_network",
            session_id=session_id,
            network_name=network_name,
        )

        try:
            network = await client.networks.get(network_name)
            await network.delete()
            self._log.info("docker.session_network_removed", network_name=network_name)
        except DockerError as e:
            if e.status == 404:
                self._log.warning(
                    "docker.session_network_not_found",
                    network_name=network_name,
                )
            else:
                self._log.error(
                    "docker.session_network_remove_failed",
                    network_name=network_name,
                    error=str(e),
                )
                raise

    def _build_container_config(
        self,
        spec: "ContainerSpec",
        *,
        session: "Session",
        cargo: "Cargo",
        network_name: str,
        extra_labels: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Build Docker container config for a single ContainerSpec.

        Returns:
            Tuple of (docker_config, container_name)
        """
        settings = get_settings()
        gc_instance_id = settings.gc.get_instance_id()

        # Container name: bay-{session_id}-{spec.name}
        container_name = f"bay-{session.id}-{spec.name}"

        # Labels
        container_labels = {
            "bay.owner": "default",
            "bay.sandbox_id": session.sandbox_id,
            "bay.session_id": session.id,
            "bay.cargo_id": cargo.id,
            "bay.profile_id": session.profile_id,
            "bay.runtime_port": str(spec.runtime_port),
            "bay.container_name": spec.name,
            "bay.runtime_type": spec.runtime_type,
            "bay.instance_id": gc_instance_id,
            "bay.managed": "true",
        }
        if extra_labels:
            container_labels.update(extra_labels)

        # Resource limits
        mem_limit = _parse_memory(spec.resources.memory)
        nano_cpus = int(spec.resources.cpus * 1e9)

        # Environment variables
        env = [f"{k}={v}" for k, v in spec.env.items()]
        env.extend(
            [
                f"BAY_SESSION_ID={session.id}",
                f"BAY_SANDBOX_ID={session.sandbox_id}",
                f"BAY_WORKSPACE_PATH={WORKSPACE_MOUNT_PATH}",
                f"BAY_CONTAINER_NAME={spec.name}",
            ]
        )

        # Host config
        host_config: dict[str, Any] = {
            "Binds": [f"{cargo.driver_ref}:{WORKSPACE_MOUNT_PATH}:rw"],
            "Memory": mem_limit,
            "NanoCpus": nano_cpus,
            "PidsLimit": 256,
            "NetworkMode": network_name,
        }

        # Port publishing for Bay -> container access
        expose_key = f"{spec.runtime_port}/tcp"
        exposed_ports: dict[str, dict[str, Any]] = {expose_key: {}}

        publish = bool(self._publish_ports) and self._connect_mode in ("host_port", "auto")
        if publish:
            # Use ephemeral port (0) for multi-container to avoid conflicts
            port_bindings = {
                expose_key: [
                    {
                        "HostIp": "0.0.0.0",
                        "HostPort": "",  # Let Docker assign random port
                    }
                ]
            }
            host_config["PortBindings"] = port_bindings

        # Networking config with alias = container spec name
        networking_config = {
            "EndpointsConfig": {
                network_name: {
                    "Aliases": [spec.name],
                }
            }
        }

        config: dict[str, Any] = {
            "Image": spec.image,
            "Env": env,
            "Labels": container_labels,
            "HostConfig": host_config,
            "ExposedPorts": exposed_ports,
            "Hostname": spec.name,
            "NetworkingConfig": networking_config,
        }

        return config, container_name

    async def create_multi(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
        *,
        network_name: str,
        labels: dict[str, str] | None = None,
    ) -> list[MultiContainerInfo]:
        """Create multiple containers for a session.

        Phase 2: Creates one container per ContainerSpec, all on the same
        network and sharing the same cargo volume.

        Args:
            session: Session model
            profile: Profile configuration
            cargo: Cargo to mount
            network_name: Session network name
            labels: Additional labels

        Returns:
            List of MultiContainerInfo (containers created but NOT started)
        """
        client = await self._get_client()
        containers_specs = profile.get_containers()

        self._log.info(
            "docker.create_multi",
            session_id=session.id,
            container_count=len(containers_specs),
            network=network_name,
        )

        results: list[MultiContainerInfo] = []

        for spec in containers_specs:
            config, container_name = self._build_container_config(
                spec,
                session=session,
                cargo=cargo,
                network_name=network_name,
                extra_labels=labels,
            )

            self._log.info(
                "docker.create_multi.container",
                session_id=session.id,
                container_name=container_name,
                image=spec.image,
                runtime_type=spec.runtime_type,
            )

            try:
                container = await client.containers.create(
                    config=config,
                    name=container_name,
                )
                results.append(
                    MultiContainerInfo(
                        name=spec.name,
                        container_id=container.id,
                        runtime_type=spec.runtime_type,
                        capabilities=list(spec.capabilities),
                        status=ContainerStatus.CREATED,
                    )
                )
            except Exception as e:
                self._log.error(
                    "docker.create_multi.container_failed",
                    session_id=session.id,
                    container_name=container_name,
                    error=str(e),
                )
                # Rollback: destroy all already-created containers
                for created in results:
                    try:
                        await self.destroy(created.container_id)
                    except Exception as cleanup_err:
                        self._log.warning(
                            "docker.create_multi.rollback_failed",
                            container_id=created.container_id,
                            error=str(cleanup_err),
                        )
                raise

        self._log.info(
            "docker.create_multi.done",
            session_id=session.id,
            created=[c.name for c in results],
        )

        return results

    async def _start_single_container(
        self,
        info: MultiContainerInfo,
    ) -> MultiContainerInfo:
        """Start a single container and resolve its endpoint.

        Internal helper for start_multi.
        """
        client = await self._get_client()

        container = client.containers.container(info.container_id)
        await container.start()

        docker_info = await container.show()
        runtime_port = int(
            docker_info.get("Config", {}).get("Labels", {}).get("bay.runtime_port", "8123")
        )

        # Resolve endpoint (same logic as start())
        endpoint: str | None = None

        if self._connect_mode in ("container_network", "auto"):
            ip = self._resolve_container_ip(docker_info)
            if ip:
                endpoint = self._endpoint_from_container_ip(ip, runtime_port)

        if endpoint is None and self._connect_mode in ("host_port", "auto"):
            hp = self._resolve_host_port(docker_info, runtime_port=runtime_port)
            if hp:
                host, port = hp
                endpoint = self._endpoint_from_hostport(host, port)

        if endpoint is None:
            # Last resort: container hostname
            name = docker_info.get("Name", "").lstrip("/")
            endpoint = f"http://{name}:{runtime_port}"
            self._log.warning("docker.start_multi.fallback_name", endpoint=endpoint)

        info.endpoint = endpoint
        info.status = ContainerStatus.RUNNING

        self._log.info(
            "docker.start_multi.container_started",
            container_name=info.name,
            container_id=info.container_id,
            endpoint=endpoint,
        )

        return info

    async def start_multi(
        self,
        containers: list[MultiContainerInfo],
    ) -> list[MultiContainerInfo]:
        """Start multiple containers in parallel.

        Phase 2: Uses asyncio.gather for parallel startup.

        Args:
            containers: List of MultiContainerInfo from create_multi

        Returns:
            Updated list with endpoints and RUNNING status
        """
        self._log.info(
            "docker.start_multi",
            container_names=[c.name for c in containers],
        )

        tasks = [self._start_single_container(c) for c in containers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check for failures
        started: list[MultiContainerInfo] = []
        errors: list[tuple[str, Exception]] = []

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                errors.append((containers[i].name, result))
            else:
                started.append(result)

        if errors:
            error_names = [name for name, _ in errors]
            self._log.error(
                "docker.start_multi.partial_failure",
                failed=error_names,
                started=[c.name for c in started],
            )
            # Per decision: all-or-nothing rollback
            # Stop/destroy all started containers
            for c in started:
                try:
                    await self.destroy(c.container_id)
                except Exception as cleanup_err:
                    self._log.warning(
                        "docker.start_multi.rollback_failed",
                        container_id=c.container_id,
                        error=str(cleanup_err),
                    )
            # Re-raise the first error
            raise errors[0][1]

        self._log.info(
            "docker.start_multi.done",
            started=[c.name for c in started],
        )

        return started

    async def stop_multi(self, containers: list[MultiContainerInfo]) -> None:
        """Stop multiple containers, best-effort.

        Args:
            containers: List of MultiContainerInfo to stop
        """
        self._log.info(
            "docker.stop_multi",
            container_names=[c.name for c in containers],
        )

        for c in containers:
            try:
                await self.stop(c.container_id)
            except Exception as e:
                self._log.warning(
                    "docker.stop_multi.container_failed",
                    container_name=c.name,
                    container_id=c.container_id,
                    error=str(e),
                )

    async def destroy_multi(self, containers: list[MultiContainerInfo]) -> None:
        """Destroy (remove) multiple containers, best-effort.

        Args:
            containers: List of MultiContainerInfo to destroy
        """
        self._log.info(
            "docker.destroy_multi",
            container_names=[c.name for c in containers],
        )

        for c in containers:
            try:
                await self.destroy(c.container_id)
            except Exception as e:
                self._log.warning(
                    "docker.destroy_multi.container_failed",
                    container_name=c.name,
                    container_id=c.container_id,
                    error=str(e),
                )
