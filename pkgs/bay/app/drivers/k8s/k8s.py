"""Kubernetes driver implementation using kubernetes-asyncio.

Bay acts as the only external gateway. Ship Pods communicate via Pod IP directly.
No Service/Ingress needed for individual Ship Pods.

Architecture:
    Client -> Bay (Ingress/LB exposed) -> Pod IP:runtime_port

See: plans/phase-2/k8s-driver-analysis.md
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiClient, ApiException

from app.config import get_settings
from app.drivers.base import ContainerInfo, ContainerStatus, Driver, RuntimeInstance

if TYPE_CHECKING:
    from app.config import ProfileConfig
    from app.models.cargo import Cargo
    from app.models.session import Session

logger = structlog.get_logger()

# Cargo mount path inside container (fixed, same as Docker)
WORKSPACE_MOUNT_PATH = "/workspace"


def _parse_storage_size(size_str: str) -> str:
    """Normalize storage size string for K8s (e.g., '1g' -> '1Gi').

    K8s uses binary units (Ki, Mi, Gi) while users may input decimal (k, m, g).
    """
    size_str = size_str.strip()
    # Already in K8s format
    if size_str.endswith(("Ki", "Mi", "Gi", "Ti")):
        return size_str
    # Convert common formats
    if size_str.lower().endswith("g"):
        return f"{size_str[:-1]}Gi"
    if size_str.lower().endswith("m"):
        return f"{size_str[:-1]}Mi"
    if size_str.lower().endswith("k"):
        return f"{size_str[:-1]}Ki"
    return size_str


def _parse_memory(memory_str: str) -> str:
    """Normalize memory string for K8s (e.g., '1g' -> '1Gi', '512m' -> '512Mi').

    K8s uses binary units (Ki, Mi, Gi) while config may use lowercase (k, m, g).
    This is the same logic as _parse_storage_size but kept separate for clarity.
    """
    return _parse_storage_size(memory_str)


class K8sDriver(Driver):
    """Kubernetes driver implementation using kubernetes-asyncio.

    Creates Pods for sessions and PVCs for cargo storage.
    Bay reaches Ship Pods via Pod IP directly (no Service needed).
    """

    def __init__(self) -> None:
        settings = get_settings()
        k8s_cfg = settings.driver.k8s

        self._namespace = k8s_cfg.namespace
        self._kubeconfig = k8s_cfg.kubeconfig
        self._storage_class = k8s_cfg.storage_class
        self._default_storage_size = k8s_cfg.default_storage_size
        self._image_pull_secrets = k8s_cfg.image_pull_secrets
        self._pod_startup_timeout = k8s_cfg.pod_startup_timeout
        self._label_prefix = k8s_cfg.label_prefix

        self._log = logger.bind(driver="k8s")
        self._api_client: ApiClient | None = None
        self._config_loaded = False

    def _label(self, key: str) -> str:
        """Build label key with prefix (e.g., 'bay.session_id')."""
        return f"{self._label_prefix}.{key}"

    async def _ensure_config(self) -> None:
        """Load Kubernetes configuration once."""
        if self._config_loaded:
            return

        if self._kubeconfig:
            await config.load_kube_config(config_file=self._kubeconfig)
            self._log.info("k8s.config.loaded", source="kubeconfig", path=self._kubeconfig)
        else:
            config.load_incluster_config()
            self._log.info("k8s.config.loaded", source="incluster")

        self._config_loaded = True

    async def _get_api_client(self) -> ApiClient:
        """Get or create the API client."""
        await self._ensure_config()
        if self._api_client is None:
            self._api_client = ApiClient()
        return self._api_client

    async def close(self) -> None:
        """Close the API client."""
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None

    def _build_labels(
        self,
        session: "Session",
        cargo: "Cargo",
        profile_id: str,
        runtime_port: int,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build standard labels for Pod/PVC."""
        settings = get_settings()
        gc_instance_id = settings.gc.get_instance_id()

        labels = {
            self._label("owner"): "default",
            self._label("sandbox_id"): session.sandbox_id,
            self._label("session_id"): session.id,
            self._label("cargo_id"): cargo.id,
            self._label("profile_id"): profile_id,
            self._label("runtime_port"): str(runtime_port),
            self._label("instance_id"): gc_instance_id,
            self._label("managed"): "true",
        }
        if extra:
            labels.update(extra)
        return labels

    async def create(
        self,
        session: "Session",
        profile: "ProfileConfig",
        cargo: "Cargo",
        *,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create a Pod without waiting for it to start.

        In K8s, Pods start automatically after creation, but we don't wait here.
        The start() method will wait for the Pod to be ready.

        Returns:
            Pod name (used as container_id)
        """
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        runtime_port = int(profile.runtime_port or 8000)
        pod_name = f"bay-session-{session.id}"

        # Build labels
        pod_labels = self._build_labels(
            session=session,
            cargo=cargo,
            profile_id=profile.id,
            runtime_port=runtime_port,
            extra=labels,
        )

        # Build environment variables
        env = [
            client.V1EnvVar(name=k, value=v)
            for k, v in profile.env.items()
        ]
        env.extend([
            client.V1EnvVar(name="BAY_SESSION_ID", value=session.id),
            client.V1EnvVar(name="BAY_SANDBOX_ID", value=session.sandbox_id),
            client.V1EnvVar(name="BAY_WORKSPACE_PATH", value=WORKSPACE_MOUNT_PATH),
        ])

        # Build resource requirements
        memory_k8s = _parse_memory(profile.resources.memory)
        resources = client.V1ResourceRequirements(
            limits={
                "cpu": str(profile.resources.cpus),
                "memory": memory_k8s,
            },
            requests={
                "cpu": str(profile.resources.cpus / 2),  # Request half of limit
                "memory": memory_k8s,
            },
        )

        # Build container spec
        container = client.V1Container(
            name="ship",
            image=profile.image,
            image_pull_policy="IfNotPresent",  # Use local image; required for kind/docker-desktop
            ports=[client.V1ContainerPort(container_port=runtime_port)],
            env=env,
            volume_mounts=[
                client.V1VolumeMount(
                    name="workspace",
                    mount_path=WORKSPACE_MOUNT_PATH,
                )
            ],
            resources=resources,
        )

        # Build volume spec (mount PVC)
        volumes = [
            client.V1Volume(
                name="workspace",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=cargo.driver_ref,
                ),
            )
        ]

        # Build image pull secrets
        image_pull_secrets = None
        if self._image_pull_secrets:
            image_pull_secrets = [
                client.V1LocalObjectReference(name=secret)
                for secret in self._image_pull_secrets
            ]

        # Build Pod spec
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self._namespace,
                labels=pod_labels,
            ),
            spec=client.V1PodSpec(
                containers=[container],
                volumes=volumes,
                image_pull_secrets=image_pull_secrets,
                restart_policy="Never",  # Don't restart crashed pods
            ),
        )

        self._log.info(
            "k8s.create",
            pod_name=pod_name,
            session_id=session.id,
            image=profile.image,
            cargo=cargo.driver_ref,
            runtime_port=runtime_port,
        )

        try:
            await v1.create_namespaced_pod(namespace=self._namespace, body=pod)
        except ApiException as e:
            if e.status == 409:  # Already exists
                self._log.warning("k8s.create.already_exists", pod_name=pod_name)
            else:
                raise

        return pod_name

    async def start(self, container_id: str, *, runtime_port: int) -> str:
        """Wait for Pod to be Running and return endpoint.

        K8s Pods start automatically after creation. This method waits
        for the Pod to reach Running phase and have an IP assigned.

        Returns:
            Runtime endpoint URL (http://<pod_ip>:<runtime_port>)
        """
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        self._log.info(
            "k8s.start",
            pod_name=container_id,
            runtime_port=runtime_port,
            timeout=self._pod_startup_timeout,
        )

        # Poll until Pod is ready
        for i in range(self._pod_startup_timeout):
            try:
                pod = await v1.read_namespaced_pod(
                    name=container_id,
                    namespace=self._namespace,
                )

                phase = pod.status.phase
                pod_ip = pod.status.pod_ip

                if phase == "Running" and pod_ip:
                    endpoint = f"http://{pod_ip}:{runtime_port}"
                    self._log.info("k8s.start.ready", pod_name=container_id, endpoint=endpoint)
                    return endpoint

                if phase in ("Failed", "Succeeded"):
                    # Pod terminated unexpectedly
                    raise RuntimeError(
                        f"Pod {container_id} terminated with phase: {phase}"
                    )

                self._log.debug(
                    "k8s.start.waiting",
                    pod_name=container_id,
                    phase=phase,
                    attempt=i + 1,
                )

            except ApiException as e:
                if e.status == 404:
                    raise RuntimeError(f"Pod {container_id} not found")
                raise

            await asyncio.sleep(1)

        raise RuntimeError(
            f"Pod {container_id} failed to start within {self._pod_startup_timeout}s"
        )

    async def stop(self, container_id: str) -> None:
        """Stop a Pod.

        In K8s, there's no stop/pause concept. We delete the Pod.
        For graceful shutdown, the Pod's terminationGracePeriodSeconds is respected.
        """
        # In K8s, stop = delete (no pause concept)
        await self.destroy(container_id)

    async def destroy(self, container_id: str) -> None:
        """Delete a Pod."""
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        self._log.info("k8s.destroy", pod_name=container_id)

        try:
            await v1.delete_namespaced_pod(
                name=container_id,
                namespace=self._namespace,
                body=client.V1DeleteOptions(
                    grace_period_seconds=10,
                ),
            )
        except ApiException as e:
            if e.status == 404:
                self._log.warning("k8s.destroy.not_found", pod_name=container_id)
            else:
                raise

    async def status(
        self, container_id: str, *, runtime_port: int | None = None
    ) -> ContainerInfo:
        """Get Pod status."""
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        try:
            pod = await v1.read_namespaced_pod(
                name=container_id,
                namespace=self._namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return ContainerInfo(
                    container_id=container_id,
                    status=ContainerStatus.NOT_FOUND,
                )
            raise

        # Map Pod phase to ContainerStatus
        phase = pod.status.phase
        container_statuses = pod.status.container_statuses or []

        if phase == "Running":
            status = ContainerStatus.RUNNING
        elif phase == "Pending":
            status = ContainerStatus.CREATED
        elif phase in ("Succeeded", "Failed"):
            status = ContainerStatus.EXITED
        else:
            status = ContainerStatus.EXITED

        # Get endpoint if running
        endpoint = None
        if status == ContainerStatus.RUNNING and runtime_port and pod.status.pod_ip:
            endpoint = f"http://{pod.status.pod_ip}:{runtime_port}"

        # Get exit code from container status
        exit_code = None
        if container_statuses:
            cs = container_statuses[0]
            if cs.state and cs.state.terminated:
                exit_code = cs.state.terminated.exit_code

        return ContainerInfo(
            container_id=container_id,
            status=status,
            endpoint=endpoint,
            exit_code=exit_code,
        )

    async def logs(self, container_id: str, tail: int = 100) -> str:
        """Get Pod logs."""
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        try:
            logs = await v1.read_namespaced_pod_log(
                name=container_id,
                namespace=self._namespace,
                container="ship",
                tail_lines=tail,
            )
            return logs or ""
        except ApiException as e:
            if e.status == 404:
                return ""
            raise

    # Volume management (PVC)

    async def create_volume(self, name: str, labels: dict[str, str] | None = None) -> str:
        """Create a PersistentVolumeClaim for cargo storage."""
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        pvc_labels = {self._label("managed"): "true"}
        if labels:
            pvc_labels.update(labels)

        storage_size = _parse_storage_size(self._default_storage_size)

        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self._namespace,
                labels=pvc_labels,
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": storage_size},
                ),
                storage_class_name=self._storage_class,
            ),
        )

        self._log.info(
            "k8s.create_volume",
            name=name,
            storage_size=storage_size,
            storage_class=self._storage_class,
        )

        try:
            await v1.create_namespaced_persistent_volume_claim(
                namespace=self._namespace,
                body=pvc,
            )
        except ApiException as e:
            if e.status == 409:  # Already exists
                self._log.warning("k8s.create_volume.already_exists", name=name)
            else:
                raise

        return name

    async def delete_volume(self, name: str) -> None:
        """Delete a PersistentVolumeClaim."""
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        self._log.info("k8s.delete_volume", name=name)

        try:
            await v1.delete_namespaced_persistent_volume_claim(
                name=name,
                namespace=self._namespace,
            )
        except ApiException as e:
            if e.status == 404:
                self._log.warning("k8s.delete_volume.not_found", name=name)
            else:
                raise

    async def volume_exists(self, name: str) -> bool:
        """Check if a PVC exists."""
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        try:
            await v1.read_namespaced_persistent_volume_claim(
                name=name,
                namespace=self._namespace,
            )
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    # Runtime instance discovery (for GC)

    async def list_runtime_instances(
        self, *, labels: dict[str, str]
    ) -> list[RuntimeInstance]:
        """List Pods matching labels.

        Used by OrphanContainerGC to discover Pods that may be orphaned.
        """
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        # Build label selector: key1=value1,key2=value2
        label_selector = ",".join(f"{k}={v}" for k, v in labels.items())

        self._log.debug(
            "k8s.list_runtime_instances",
            label_selector=label_selector,
        )

        try:
            pod_list = await v1.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=label_selector,
            )
        except ApiException:
            self._log.exception("k8s.list_runtime_instances.failed")
            return []

        instances = []
        for pod in pod_list.items:
            # Get creation timestamp
            created_at = None
            if pod.metadata.creation_timestamp:
                created_at = pod.metadata.creation_timestamp.isoformat()

            # Get state from phase
            state = pod.status.phase.lower() if pod.status.phase else "unknown"

            instances.append(
                RuntimeInstance(
                    id=pod.metadata.name,
                    name=pod.metadata.name,
                    labels=pod.metadata.labels or {},
                    state=state,
                    created_at=created_at,
                )
            )

        self._log.debug(
            "k8s.list_runtime_instances.result",
            count=len(instances),
        )

        return instances

    async def destroy_runtime_instance(self, instance_id: str) -> None:
        """Force delete a Pod.

        Used by OrphanContainerGC to clean up orphan Pods.
        """
        api_client = await self._get_api_client()
        v1 = client.CoreV1Api(api_client)

        self._log.info("k8s.destroy_runtime_instance", pod_name=instance_id)

        try:
            await v1.delete_namespaced_pod(
                name=instance_id,
                namespace=self._namespace,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0,  # Force delete
                ),
            )
        except ApiException as e:
            if e.status == 404:
                self._log.warning(
                    "k8s.destroy_runtime_instance.not_found",
                    pod_name=instance_id,
                )
            else:
                raise
