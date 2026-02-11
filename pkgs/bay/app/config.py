"""Bay configuration management.

Configuration sources (in priority order):
1. Environment variables (BAY_ prefix)
2. Config file (config.yaml)
3. Defaults
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseModel):
    """HTTP server configuration."""

    host: str = "0.0.0.0"
    port: int = 8000


class DatabaseConfig(BaseModel):
    """Database configuration."""

    # Phase 1: SQLite; 可切换到 postgresql+asyncpg:// 或 mysql+asyncmy://
    url: str = "sqlite+aiosqlite:///./bay.db"
    echo: bool = False


class DockerConfig(BaseModel):
    """Docker driver configuration."""

    socket: str = "unix:///var/run/docker.sock"

    # 可选：把 runtime 容器接入指定 network（Bay 也需要在该 network 内才能用容器 IP 直连）
    # 为空则不指定 network（使用 Docker 默认网络）
    network: str | None = None

    # Bay->Runtime 连接模式：
    # - container_network: 使用容器网络 IP 直连（需要 network 且 Bay 可达）
    # - host_port: 使用宿主机端口映射（Bay 在宿主机上最常见）
    # - auto: 优先 container_network，失败则回退 host_port
    connect_mode: Literal["container_network", "host_port", "auto"] = "auto"

    # host_port 模式下，Bay 连接 runtime 的 host 地址
    host_address: str = "127.0.0.1"

    # host_port 模式下，是否发布端口；auto 模式回退也依赖它
    publish_ports: bool = True

    # 指定固定宿主机端口（None/0 表示随机端口）
    host_port: int | None = None


class K8sConfig(BaseModel):
    """Kubernetes driver configuration (Phase 2).

    Bay acts as the only external gateway. Ship Pods communicate via Pod IP directly.
    No Service/Ingress needed for individual Ship Pods.
    """

    namespace: str = "bay"
    kubeconfig: str | None = None  # None = in-cluster config

    # PVC storage class (None = use cluster default)
    storage_class: str | None = None

    # Default storage size for PVC
    default_storage_size: str = "1Gi"

    # Image pull secrets (for private registries)
    image_pull_secrets: list[str] = Field(default_factory=list)

    # Pod startup timeout in seconds
    pod_startup_timeout: int = 60

    # Pod labels prefix (for filtering)
    label_prefix: str = "bay"


class DriverConfig(BaseModel):
    """Driver layer configuration."""

    type: Literal["docker", "k8s"] = "docker"
    docker: DockerConfig = Field(default_factory=DockerConfig)
    k8s: K8sConfig = Field(default_factory=K8sConfig)

    # Image pull policy for runtime containers (applies to both Docker and K8s drivers).
    # - "always": Always pull the image before creating a container (ensures latest).
    # - "if_not_present": Only pull if the image is not available locally (default).
    # - "never": Never pull; fail if the image is not available locally.
    image_pull_policy: Literal["always", "if_not_present", "never"] = "if_not_present"


class ResourceSpec(BaseModel):
    """Container resource specification."""

    cpus: float = 1.0
    memory: str = "1g"


class ContainerSpec(BaseModel):
    """Single container specification within a Profile.

    Defines one container in a multi-container Sandbox, including its image,
    runtime type, resource limits, capabilities, and environment variables.
    """

    name: str  # Container name, unique within a Profile (e.g., "ship", "browser")
    image: str  # Container image (e.g., "ship:latest", "browser-runtime:latest")
    runtime_type: str = "ship"  # ship | browser | custom
    runtime_port: int = 8123  # HTTP port inside the container

    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    capabilities: list[str] = Field(default_factory=list)

    # Primary handler for these capabilities (used for conflict resolution)
    primary_for: list[str] = Field(default_factory=list)

    # Environment variables, supports ${VAR} placeholders
    env: dict[str, str] = Field(default_factory=dict)

    # Health check endpoint path
    health_check_path: str = "/health"


class StartupConfig(BaseModel):
    """Container startup strategy."""

    # Startup order: parallel (all at once) | sequential (in containers array order)
    order: Literal["parallel", "sequential"] = "parallel"

    # Whether to wait for all containers to be ready before Session is considered Ready
    wait_for_all: bool = True


class ProfileConfig(BaseModel):
    """Runtime profile configuration (supports single and multi-container).

    Phase 2: Extended to support multi-container Sandboxes via `containers` field.
    Backward compatible - old single-container format (using `image` field) is
    automatically normalized to a single-element `containers` array.

    Note:
    - `runtime_type` 决定使用哪个 Adapter 与运行时通信（如 ship, browser 等）。
    - `runtime_port` 是运行时容器对外提供 HTTP API 的容器内端口。
      * Ship 默认通常为 8000，但不应写死，必须可配置。
      * 在 DockerDriver 中可选择走"容器网络直连"或"宿主机端口映射"。
    """

    id: str
    description: str | None = None

    # ========== Phase 1 compatible fields (single-container mode) ==========
    image: str | None = None

    # 运行时类型，决定使用哪个 Adapter（如 ShipAdapter）
    # 支持的类型：ship（默认）、browser（未来）、gpu（未来）
    runtime_type: str | None = None

    resources: ResourceSpec | None = None
    capabilities: list[str] | None = None

    # 容器内运行时 HTTP 端口（用于 Bay->Runtime 访问）
    # Ship 当前默认监听 8123（见 ship 容器启动日志），因此这里给出默认 8123；
    # 但推荐在 config.yaml 里显式配置。
    runtime_port: int | None = None

    env: dict[str, str] | None = None

    # ========== Phase 2 new fields (multi-container mode) ==========
    containers: list[ContainerSpec] | None = None
    startup: StartupConfig = Field(default_factory=StartupConfig)

    # ========== Shared configuration ==========
    idle_timeout: int = 1800  # 30 minutes

    def model_post_init(self, __context: Any) -> None:
        """Normalize single-container format to multi-container format.

        Backward compatibility: if `image` field is set (old format),
        auto-convert to a single-element `containers` array.
        """
        if self.containers is not None:
            # Already multi-container format - clear legacy fields
            return

        # Single-container format → convert to multi-container
        if self.image is not None:
            container = ContainerSpec(
                name="primary",
                image=self.image,
                runtime_type=self.runtime_type or "ship",
                runtime_port=self.runtime_port or 8123,
                resources=self.resources or ResourceSpec(),
                capabilities=self.capabilities or ["filesystem", "shell", "python"],
                primary_for=self.capabilities or ["filesystem", "shell", "python"],
                env=self.env or {},
            )
            self.containers = [container]
        else:
            # Neither image nor containers specified - use default
            container = ContainerSpec(
                name="primary",
                image="ship:latest",
                runtime_type="ship",
                runtime_port=8123,
                resources=ResourceSpec(),
                capabilities=["filesystem", "shell", "python"],
                primary_for=["filesystem", "shell", "python"],
            )
            self.containers = [container]

    def get_containers(self) -> list[ContainerSpec]:
        """Get container list (always returns normalized multi-container format)."""
        return self.containers or []

    def get_primary_container(self) -> ContainerSpec | None:
        """Get primary container.

        Priority:
        1. Container named 'primary' or 'ship'
        2. First container in the list
        """
        containers = self.get_containers()
        if not containers:
            return None

        for c in containers:
            if c.name in ("primary", "ship"):
                return c

        return containers[0]

    def find_container_for_capability(self, capability: str) -> ContainerSpec | None:
        """Find the container responsible for a capability.

        Priority:
        1. Container with `primary_for` containing the capability
        2. First container with `capabilities` containing the capability
        """
        containers = self.get_containers()

        # 1. Check primary_for
        for c in containers:
            if capability in c.primary_for:
                return c

        # 2. Check capabilities
        for c in containers:
            if capability in c.capabilities:
                return c

        return None

    def get_all_capabilities(self) -> set[str]:
        """Get all capabilities supported by this Profile."""
        caps: set[str] = set()
        for c in self.get_containers():
            caps.update(c.capabilities)
        return caps


class CargoConfig(BaseModel):
    """Cargo storage configuration."""

    # 宿主机路径，仅用于 Bay 管理，不暴露给运行时
    root_path: str = "/var/lib/bay/cargos"
    default_size_limit_mb: int = 1024
    # 容器内挂载路径 (固定)
    mount_path: str = "/workspace"


class IdempotencyConfig(BaseModel):
    """Idempotency layer configuration."""

    enabled: bool = True
    ttl_hours: int = 1  # How long to keep idempotency keys


class GCTaskConfig(BaseModel):
    """GC task-specific configuration."""

    enabled: bool = True


class GCConfig(BaseModel):
    """Garbage collection configuration.

    Note on instance_id:
    - Used by OrphanContainerGC Strict mode to prevent accidental deletion
      of containers belonging to other Bay instances.
    - In single-instance deployments, the default is sufficient.
    - In multi-instance deployments, each instance MUST have a unique instance_id.
    """

    enabled: bool = True
    run_on_startup: bool = True
    interval_seconds: int = 300  # 5 minutes

    # Instance identifier for strict orphan container detection.
    # Containers with bay.instance_id != this value will NOT be touched.
    # Default derivation order:
    #   1. BAY_GC__INSTANCE_ID env var (recommended for multi-instance)
    #   2. HOSTNAME env var
    #   3. Fallback to "bay"
    instance_id: str | None = None

    # Per-task configuration
    idle_session: GCTaskConfig = Field(default_factory=GCTaskConfig)
    expired_sandbox: GCTaskConfig = Field(default_factory=GCTaskConfig)
    orphan_cargo: GCTaskConfig = Field(default_factory=GCTaskConfig)
    # OrphanContainerGC is disabled by default due to strict safety requirements
    orphan_container: GCTaskConfig = Field(default_factory=lambda: GCTaskConfig(enabled=False))

    def get_instance_id(self) -> str:
        """Get resolved instance_id with fallback logic."""
        import os

        if self.instance_id:
            return self.instance_id
        return os.environ.get("HOSTNAME", "bay")


class SecurityConfig(BaseModel):
    """Security configuration."""

    # API Key authentication
    # None = no API key validation (check allow_anonymous only)
    api_key: str | None = None

    # Allow anonymous access (no authentication required)
    # Development: True (default)
    # Production: False
    allow_anonymous: bool = True

    # Network blocklist (Phase 2)
    blocked_hosts: list[str] = Field(
        default_factory=lambda: [
            "169.254.0.0/16",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )


class Settings(BaseSettings):
    """Bay application settings."""

    model_config = SettingsConfigDict(
        env_prefix="BAY_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    driver: DriverConfig = Field(default_factory=DriverConfig)
    cargo: CargoConfig = Field(default_factory=CargoConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    idempotency: IdempotencyConfig = Field(default_factory=IdempotencyConfig)
    gc: GCConfig = Field(default_factory=GCConfig)

    # Default profiles
    profiles: list[ProfileConfig] = Field(
        default_factory=lambda: [
            ProfileConfig(
                id="python-default",
                image="ship:latest",
                resources=ResourceSpec(cpus=1.0, memory="1g"),
                capabilities=["filesystem", "shell", "python"],
                idle_timeout=1800,
            ),
            ProfileConfig(
                id="python-data",
                image="ship:data",
                resources=ResourceSpec(cpus=2.0, memory="4g"),
                capabilities=["filesystem", "shell", "python"],
                idle_timeout=1800,
            ),
        ]
    )

    def get_profile(self, profile_id: str) -> ProfileConfig | None:
        """Get profile by ID."""
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None


def _load_config_file() -> dict:
    """Load configuration from YAML file if exists.

    Looks for config file in order:
    1. BAY_CONFIG_FILE environment variable
    2. ./config.yaml
    3. /etc/bay/config.yaml
    """
    import os

    config_paths = [
        os.environ.get("BAY_CONFIG_FILE"),
        Path("config.yaml"),
        Path("/etc/bay/config.yaml"),
    ]

    for path in config_paths:
        if path is None:
            continue
        path = Path(path)
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}

    return {}


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Configuration is loaded from:
    1. YAML config file (if exists)
    2. Environment variables (override)
    3. Defaults
    """
    # Load from config file first
    file_config = _load_config_file()

    # Create settings with file config as initial values
    # Environment variables will override via pydantic-settings
    return Settings(**file_config)
