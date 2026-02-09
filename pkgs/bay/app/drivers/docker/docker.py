"""Docker driver implementation using aiodocker.

Supports multiple connectivity modes between Bay and runtime containers:
- container_network: Bay reaches runtime by container IP on a docker network
- host_port: Bay reaches runtime via host port-mapping (127.0.0.1:<host_port>)
- auto: prefer container_network, fallback to host_port

This is necessary because Bay may run:
- on the host (typical): cannot directly reach container IP on a user-defined bridge
- inside a container (docker.sock mounted): can reach other containers via shared network

Note: runtime_port is provided by ProfileConfig (do not hardcode Ship port here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiodocker
import structlog
from aiodocker.exceptions import DockerError

from app.config import get_settings
from app.drivers.base import ContainerInfo, ContainerStatus, Driver, RuntimeInstance

if TYPE_CHECKING:
    from app.config import ProfileConfig
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
