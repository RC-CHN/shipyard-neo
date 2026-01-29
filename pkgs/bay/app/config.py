"""Bay configuration management.

Configuration sources (in priority order):
1. Environment variables (BAY_ prefix)
2. Config file (config.yaml)
3. Defaults
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    """Kubernetes driver configuration (Phase 2)."""

    namespace: str = "bay"
    kubeconfig: str | None = None


class DriverConfig(BaseModel):
    """Driver layer configuration."""

    type: Literal["docker", "k8s"] = "docker"
    docker: DockerConfig = Field(default_factory=DockerConfig)
    k8s: K8sConfig = Field(default_factory=K8sConfig)


class ResourceSpec(BaseModel):
    """Container resource specification."""

    cpus: float = 1.0
    memory: str = "1g"


class ProfileConfig(BaseModel):
    """Runtime profile configuration.

    Note:
    - `runtime_type` 决定使用哪个 Adapter 与运行时通信（如 ship, browser 等）。
    - `runtime_port` 是运行时容器对外提供 HTTP API 的容器内端口。
      * Ship 默认通常为 8000，但不应写死，必须可配置。
      * 在 DockerDriver 中可选择走"容器网络直连"或"宿主机端口映射"。
    """

    id: str
    image: str = "ship:latest"

    # 运行时类型，决定使用哪个 Adapter（如 ShipAdapter）
    # 支持的类型：ship（默认）、browser（未来）、gpu（未来）
    runtime_type: str = "ship"

    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    capabilities: list[str] = Field(default_factory=lambda: ["filesystem", "shell", "python"])
    idle_timeout: int = 1800  # 30 minutes

    # 容器内运行时 HTTP 端口（用于 Bay->Runtime 访问）
    # Ship 当前默认监听 8123（见 ship 容器启动日志），因此这里给出默认 8123，但推荐在 config.yaml 里显式配置。
    runtime_port: int | None = 8123

    env: dict[str, str] = Field(default_factory=dict)


class WorkspaceConfig(BaseModel):
    """Workspace storage configuration."""

    # 宿主机路径，仅用于 Bay 管理，不暴露给运行时
    root_path: str = "/var/lib/bay/workspaces"
    default_size_limit_mb: int = 1024
    # 容器内挂载路径 (固定)
    mount_path: str = "/workspace"


class IdempotencyConfig(BaseModel):
    """Idempotency layer configuration."""

    enabled: bool = True
    ttl_hours: int = 1  # How long to keep idempotency keys


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
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    idempotency: IdempotencyConfig = Field(default_factory=IdempotencyConfig)

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
