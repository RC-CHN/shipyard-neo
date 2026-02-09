# Profile Schema V2：多容器支持

> 状态：**Draft**
> 日期：2026-02-09

## 1. 设计目标

扩展 Profile 配置以支持多容器 Sandbox：

1. **向后兼容**：现有单容器配置无需修改
2. **灵活组合**：允许任意容器组合（Ship + Browser、Ship + GPU 等）
3. **能力路由**：明确定义每个容器提供的能力及冲突解决
4. **资源隔离**：每个容器独立配置资源限制

## 2. 当前 Schema（Phase 1）

```yaml
# 单容器模式
profiles:
  - id: python-default
    image: "ship:latest"
    runtime_type: ship
    runtime_port: 8123
    resources:
      cpus: 1.0
      memory: "1g"
    capabilities:
      - filesystem
      - shell
      - python
    idle_timeout: 1800
    env: {}
```

### 2.1 当前数据模型

```python
class ProfileConfig(BaseModel):
    id: str
    image: str = "ship:latest"
    runtime_type: str = "ship"
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    capabilities: list[str] = Field(default_factory=lambda: ["filesystem", "shell", "python"])
    idle_timeout: int = 1800
    runtime_port: int | None = 8123
    env: dict[str, str] = Field(default_factory=dict)
```

## 3. 新 Schema 设计（Phase 2）

### 3.1 设计思路

引入 `containers` 数组替代单一 `image`，同时保持向后兼容：

```yaml
profiles:
  # 新格式：多容器
  - id: browser-python
    containers:
      - name: ship
        image: "ship:latest"
        ...
      - name: browser
        image: "browser-runtime:latest"
        ...
    
  # 旧格式：单容器（自动转换）
  - id: python-default
    image: "ship:latest"
    ...
```

### 3.2 完整 Schema 定义

```yaml
# Profile Schema V2

profiles:
  # ============================================
  # 示例 1：纯 Python 环境（单容器，向后兼容）
  # ============================================
  - id: python-default
    image: "ship:latest"           # 兼容字段
    runtime_type: ship             # 兼容字段
    runtime_port: 8123             # 兼容字段
    resources:
      cpus: 1.0
      memory: "1g"
    capabilities:
      - filesystem
      - shell
      - python
    idle_timeout: 1800
    env: {}

  # ============================================
  # 示例 2：Browser + Python 协作环境（多容器）
  # ============================================
  - id: browser-python
    description: "Browser automation with Python backend"
    
    # 多容器定义
    containers:
      # 主容器：Ship
      - name: ship
        image: "ship:latest"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 1.0
          memory: "1g"
        capabilities:
          - python
          - shell
          - filesystem
          - upload
          - download
        primary_for:
          - filesystem    # 文件操作优先走 Ship
          - upload
          - download
        env: {}
      
      # Sidecar 容器：Browser
      - name: browser
        image: "browser-runtime:latest"
        runtime_type: gull
        runtime_port: 8080
        resources:
          cpus: 1.0
          memory: "2g"
        capabilities:
          - browser
          - screenshot
          - filesystem    # Browser 也能访问文件系统
        env:
          SANDBOX_ID: "${SANDBOX_ID}"   # 注入 sandbox_id
    
    # Session 级别配置
    idle_timeout: 1800
    
    # 容器启动策略
    startup:
      order: parallel       # parallel | sequential
      wait_for_all: true    # 所有容器就绪才算 Ready

  # ============================================
  # 示例 3：多 GPU 容器
  # ============================================
  - id: multi-gpu
    containers:
      - name: ship
        image: "ship:latest"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 2.0
          memory: "8g"
        capabilities:
          - python
          - shell
          - filesystem
        primary_for:
          - filesystem
        env: {}
      
      - name: gpu-worker-1
        image: "ship:gpu"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 4.0
          memory: "16g"
        capabilities:
          - python
          - shell
        env:
          CUDA_VISIBLE_DEVICES: "0"
          WORKER_ID: "1"
      
      - name: gpu-worker-2
        image: "ship:gpu"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 4.0
          memory: "16g"
        capabilities:
          - python
          - shell
        env:
          CUDA_VISIBLE_DEVICES: "1"
          WORKER_ID: "2"
    
    idle_timeout: 3600
```

### 3.3 数据模型更新

```python
# pkgs/bay/app/config.py

from pydantic import BaseModel, Field, model_validator
from typing import Literal

class ResourceSpec(BaseModel):
    """容器资源规格。"""
    cpus: float = 1.0
    memory: str = "1g"


class ContainerSpec(BaseModel):
    """单个容器的配置规格。"""
    
    name: str                           # 容器名称，在 Profile 内唯一
    image: str                          # 容器镜像
    runtime_type: str = "ship"          # ship | gull | custom
    runtime_port: int = 8123            # 容器内 HTTP 端口
    
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    capabilities: list[str] = Field(default_factory=list)
    
    # 作为这些能力的主处理者（冲突解决）
    primary_for: list[str] = Field(default_factory=list)
    
    # 环境变量，支持 ${VAR} 占位符
    env: dict[str, str] = Field(default_factory=dict)
    
    # 健康检查路径
    health_check_path: str = "/health"


class StartupConfig(BaseModel):
    """容器启动策略。"""
    
    # 启动顺序：parallel（并行）| sequential（按 containers 顺序）
    order: Literal["parallel", "sequential"] = "parallel"
    
    # 是否等待所有容器就绪才算 Session Ready
    wait_for_all: bool = True


class ProfileConfig(BaseModel):
    """运行时 Profile 配置（支持单容器和多容器）。"""
    
    id: str
    description: str | None = None
    
    # ========== Phase 1 兼容字段（单容器模式）==========
    image: str | None = None
    runtime_type: str | None = None
    runtime_port: int | None = None
    resources: ResourceSpec | None = None
    capabilities: list[str] | None = None
    env: dict[str, str] | None = None
    
    # ========== Phase 2 新增字段（多容器模式）==========
    containers: list[ContainerSpec] | None = None
    startup: StartupConfig = Field(default_factory=StartupConfig)
    
    # ========== 共享配置 ==========
    idle_timeout: int = 1800
    
    @model_validator(mode="after")
    def normalize_to_multi_container(self) -> "ProfileConfig":
        """将单容器格式规范化为多容器格式。
        
        兼容逻辑：如果使用旧格式（image 字段），自动转换为 containers 数组。
        """
        if self.containers is not None:
            # 已经是多容器格式，清理兼容字段
            self.image = None
            self.runtime_type = None
            self.runtime_port = None
            self.resources = None
            self.capabilities = None
            self.env = None
            return self
        
        # 单容器格式 → 转换为多容器格式
        if self.image is not None:
            container = ContainerSpec(
                name="primary",  # 默认名称
                image=self.image,
                runtime_type=self.runtime_type or "ship",
                runtime_port=self.runtime_port or 8123,
                resources=self.resources or ResourceSpec(),
                capabilities=self.capabilities or ["filesystem", "shell", "python"],
                primary_for=self.capabilities or ["filesystem", "shell", "python"],
                env=self.env or {},
            )
            self.containers = [container]
        
        return self
    
    def get_containers(self) -> list[ContainerSpec]:
        """获取容器列表（始终返回规范化后的多容器格式）。"""
        return self.containers or []
    
    def get_primary_container(self) -> ContainerSpec | None:
        """获取主容器（第一个容器或名为 'primary' / 'ship' 的容器）。"""
        containers = self.get_containers()
        if not containers:
            return None
        
        # 优先查找 primary 或 ship
        for c in containers:
            if c.name in ("primary", "ship"):
                return c
        
        # 否则返回第一个
        return containers[0]
    
    def find_container_for_capability(self, capability: str) -> ContainerSpec | None:
        """根据 capability 查找容器。
        
        优先级：
        1. primary_for 包含该 capability 的容器
        2. capabilities 包含该 capability 的第一个容器
        """
        containers = self.get_containers()
        
        # 1. 查找 primary_for
        for c in containers:
            if capability in c.primary_for:
                return c
        
        # 2. 查找 capabilities
        for c in containers:
            if capability in c.capabilities:
                return c
        
        return None
    
    def get_all_capabilities(self) -> set[str]:
        """获取 Profile 支持的所有 capabilities。"""
        caps = set()
        for c in self.get_containers():
            caps.update(c.capabilities)
        return caps
```

## 4. 向后兼容性

### 4.1 兼容矩阵

| 配置格式 | 处理方式 | Session.containers |
|----------|---------|-------------------|
| 旧格式（`image` 字段） | 自动转换为单容器数组 | `[{name: "primary", ...}]` |
| 新格式（`containers` 数组） | 直接使用 | 用户定义的容器列表 |

### 4.2 示例对比

**旧格式输入**：
```yaml
profiles:
  - id: python-default
    image: "ship:latest"
    runtime_type: ship
    runtime_port: 8123
    capabilities:
      - filesystem
      - python
```

**规范化后**：
```python
ProfileConfig(
    id="python-default",
    containers=[
        ContainerSpec(
            name="primary",
            image="ship:latest",
            runtime_type="ship",
            runtime_port=8123,
            capabilities=["filesystem", "python"],
            primary_for=["filesystem", "python"],
        )
    ]
)
```

## 5. 能力路由规则

### 5.1 路由优先级

```python
def route_capability(profile: ProfileConfig, capability: str) -> ContainerSpec:
    """路由 capability 到容器。"""
    
    # 1. 查找 primary_for 声明
    for c in profile.containers:
        if capability in c.primary_for:
            return c
    
    # 2. 查找 capabilities 包含
    for c in profile.containers:
        if capability in c.capabilities:
            return c
    
    # 3. 无匹配
    raise CapabilityNotSupportedError(capability)
```

### 5.2 冲突场景示例

```yaml
containers:
  - name: ship
    capabilities: [python, shell, filesystem]
    primary_for: [filesystem]     # ← Ship 是 filesystem 的主处理者
  
  - name: browser
    capabilities: [browser, screenshot, filesystem]
    # 无 primary_for，filesystem 请求将路由到 Ship
```

**路由结果**：
- `python` → Ship（独占）
- `shell` → Ship（独占）
- `browser` → Browser（独占）
- `filesystem` → Ship（primary_for 声明）

## 6. 环境变量注入

### 6.1 支持的占位符

| 占位符 | 说明 | 示例值 |
|--------|------|--------|
| `${SANDBOX_ID}` | Sandbox ID | `sbx_abc123` |
| `${SESSION_ID}` | Session ID | `sess_xyz789` |
| `${CONTAINER_NAME}` | 容器名称 | `ship`, `browser` |
| `${CARGO_PATH}` | Cargo 挂载路径 | `/workspace` |

### 6.2 配置示例

```yaml
containers:
  - name: browser
    image: "browser-runtime:latest"
    env:
      SANDBOX_ID: "${SANDBOX_ID}"     # 用于 agent-browser --session
      DISPLAY: ":99"
      BROWSER_TIMEOUT: "30000"
```

## 7. 实现计划

### 7.1 代码变更

| 文件 | 变更 |
|------|------|
| [`app/config.py`](pkgs/bay/app/config.py) | 添加 `ContainerSpec`, `StartupConfig`，扩展 `ProfileConfig` |
| [`app/models/session.py`](pkgs/bay/app/models/session.py) | 添加 `containers` JSON 字段 |
| [`app/drivers/docker/docker.py`](pkgs/bay/app/drivers/docker/docker.py) | 支持创建多容器 + 共享网络 |
| [`app/router/capability/capability.py`](pkgs/bay/app/router/capability/capability.py) | 多容器路由逻辑 |

### 7.2 数据库迁移

```sql
-- Session 表添加 containers 字段
ALTER TABLE sessions ADD COLUMN containers JSON DEFAULT '[]';
```

## 8. 验证测试

### 8.1 单元测试

```python
def test_legacy_profile_normalization():
    """测试旧格式自动转换。"""
    config = ProfileConfig(
        id="test",
        image="ship:latest",
        capabilities=["python"],
    )
    assert len(config.get_containers()) == 1
    assert config.get_containers()[0].name == "primary"

def test_multi_container_profile():
    """测试多容器配置。"""
    config = ProfileConfig(
        id="browser-python",
        containers=[
            ContainerSpec(name="ship", image="ship:latest", capabilities=["python"]),
            ContainerSpec(name="browser", image="browser:latest", capabilities=["browser"]),
        ]
    )
    assert config.find_container_for_capability("python").name == "ship"
    assert config.find_container_for_capability("browser").name == "browser"

def test_capability_routing_with_primary():
    """测试 primary_for 路由优先级。"""
    config = ProfileConfig(
        id="test",
        containers=[
            ContainerSpec(
                name="ship",
                image="ship:latest",
                capabilities=["filesystem"],
                primary_for=["filesystem"],
            ),
            ContainerSpec(
                name="browser",
                image="browser:latest",
                capabilities=["filesystem", "browser"],
            ),
        ]
    )
    # filesystem 应该路由到 ship（因为 primary_for）
    assert config.find_container_for_capability("filesystem").name == "ship"
```

## 9. 配置迁移指南

### 9.1 无需迁移

现有配置**无需修改**，自动兼容：

```yaml
# 现有配置（继续工作）
profiles:
  - id: python-default
    image: "ship:latest"
    capabilities: [filesystem, python]
```

### 9.2 升级到多容器

如需使用多容器功能，改用新格式：

```yaml
# 升级后配置
profiles:
  - id: browser-python
    containers:
      - name: ship
        image: "ship:latest"
        capabilities: [python, shell, filesystem]
        primary_for: [filesystem]
      - name: browser
        image: "browser-runtime:latest"
        capabilities: [browser, screenshot]
```
