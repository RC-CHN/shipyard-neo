# Bay Python SDK 设计方案

> 状态：**Draft**
> 日期：2026-02-04

## 1. 概述

本文档定义 Bay 服务的 Python SDK 设计，替换旧 Shipyard SDK。

### 1.1 设计目标

1. **与 Bay API 完全对齐**：基于 `/v1/sandboxes/*` 端点，不再使用 `/ship/*` 旧路径
2. **以 Sandbox 为核心抽象**：隐藏 Session 细节，用户只需关心 Sandbox
3. **类型安全**：使用 Pydantic/dataclass 提供完整类型提示
4. **异步优先**：支持 `async/await`，同时提供同步包装器
5. **错误语义对齐**：SDK 异常与 Bay API 错误码一一映射

### 1.2 新旧 SDK 对比

| 维度 | 旧 SDK (Shipyard) | 新 SDK (Bay) |
|:--|:--|:--|
| 核心资源 | `Ship` / `SessionShip` | `Sandbox` |
| API 路径 | `/ship/*` (非标准化) | `/v1/sandboxes/*` (RESTful) |
| Session 概念 | 暴露 `session_id` | 内部化，不暴露 |
| 能力调用方式 | `ship.fs.*`, `ship.shell.*`, `ship.python.*` | `sandbox.filesystem.*`, `sandbox.shell.*`, `sandbox.python.*` |
| TTL 管理 | `ttl` 参数 + `extend_ttl()` | `ttl` 参数 + `extend_ttl()` + `keepalive()` |
| 资源规格 | `Spec(cpus, memory)` | `profile` 枚举（有限集合） |
| Workspace | 不暴露 | 可选高级 API |

## 2. 核心概念模型

### 2.1 资源层次

**BayClient（主入口）**
- 属性：`endpoint_url`, `access_token`
- 方法：`create_sandbox()`, `get_sandbox()`, `list_sandboxes()`
- 子管理器：`workspaces: WorkspaceManager`

**Sandbox（核心资源）**
- 属性：`id`, `status`, `profile`, `workspace_id`, `capabilities`, `expires_at`, `idle_expires_at`
- 能力组件：`filesystem`, `shell`, `python`
- 方法：`stop()`, `delete()`, `extend_ttl()`, `keepalive()`

**FilesystemCapability（文件系统能力）**
- 方法：`read_file()`, `write_file()`, `list_dir()`, `delete()`, `upload()`, `download()`

**ShellCapability（Shell 能力）**
- 方法：`exec()`

**PythonCapability（Python 能力）**
- 方法：`exec()`

**WorkspaceManager（Workspace 管理，高级 API）**
- 方法：`create()`, `get()`, `list()`, `delete()`

**关系**：
- BayClient 创建/管理 Sandbox
- BayClient 通过 workspaces 属性访问 WorkspaceManager
- Sandbox 包含 filesystem、shell、python 三个能力组件

### 2.2 状态枚举

```python
class SandboxStatus(str, Enum):
    IDLE = "idle"          # 无运行实例
    STARTING = "starting"  # Session 启动中
    READY = "ready"        # Session 就绪
    FAILED = "failed"      # 启动失败
    EXPIRED = "expired"    # TTL 过期
```

## 3. API 映射

### 3.1 Sandbox 管理

| SDK 方法 | HTTP API | 说明 |
|:--|:--|:--|
| `client.create_sandbox()` | `POST /v1/sandboxes` | 创建 Sandbox |
| `client.get_sandbox(id)` | `GET /v1/sandboxes/{id}` | 获取详情 |
| `client.list_sandboxes()` | `GET /v1/sandboxes` | 列表查询 |
| `sandbox.stop()` | `POST /v1/sandboxes/{id}/stop` | 回收算力 |
| `sandbox.delete()` | `DELETE /v1/sandboxes/{id}` | 彻底删除 |
| `sandbox.extend_ttl(seconds)` | `POST /v1/sandboxes/{id}/extend_ttl` | 延长 TTL |
| `sandbox.keepalive()` | `POST /v1/sandboxes/{id}/keepalive` | 保活 |

### 3.2 能力调用

| SDK 方法 | HTTP API | 说明 |
|:--|:--|:--|
| `sandbox.python.exec(code)` | `POST /v1/sandboxes/{id}/python/exec` | 执行 Python |
| `sandbox.shell.exec(command)` | `POST /v1/sandboxes/{id}/shell/exec` | 执行 Shell |
| `sandbox.filesystem.read_file(path)` | `GET /v1/sandboxes/{id}/filesystem/files?path=...` | 读取文件 |
| `sandbox.filesystem.write_file(path, content)` | `PUT /v1/sandboxes/{id}/filesystem/files` | 写入文件 |
| `sandbox.filesystem.list_dir(path)` | `GET /v1/sandboxes/{id}/filesystem/directories?path=...` | 列目录 |
| `sandbox.filesystem.delete(path)` | `DELETE /v1/sandboxes/{id}/filesystem/files?path=...` | 删除 |
| `sandbox.filesystem.upload(path, content)` | `POST /v1/sandboxes/{id}/filesystem/upload` | 上传 |
| `sandbox.filesystem.download(path)` | `GET /v1/sandboxes/{id}/filesystem/download?path=...` | 下载 |

### 3.3 Workspace 管理（高级 API）

| SDK 方法 | HTTP API | 说明 |
|:--|:--|:--|
| `client.workspaces.create()` | `POST /v1/workspaces` | 创建 external workspace |
| `client.workspaces.get(id)` | `GET /v1/workspaces/{id}` | 获取详情 |
| `client.workspaces.list()` | `GET /v1/workspaces` | 列表查询 |
| `client.workspaces.delete(id)` | `DELETE /v1/workspaces/{id}` | 删除 |

## 4. 模块结构

```
pkgs/bay-sdk/
├── pyproject.toml
├── README.md
├── bay/
│   ├── __init__.py           # 导出主要类
│   ├── client.py             # BayClient 主入口
│   ├── sandbox.py            # Sandbox 资源类
│   ├── workspace.py          # Workspace 管理
│   ├── capabilities/
│   │   ├── __init__.py
│   │   ├── base.py           # BaseCapability
│   │   ├── filesystem.py     # FilesystemCapability
│   │   ├── shell.py          # ShellCapability
│   │   └── python.py         # PythonCapability
│   ├── types.py              # 类型定义（Pydantic models）
│   ├── errors.py             # 异常类
│   └── _http.py              # HTTP 客户端封装
└── tests/
    ├── __init__.py
    ├── test_client.py
    ├── test_sandbox.py
    └── test_capabilities.py
```

## 5. 类型定义

### 5.1 请求/响应模型

```python
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class SandboxStatus(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class CreateSandboxRequest(BaseModel):
    """创建 Sandbox 请求"""
    profile: str = "python-default"
    workspace_id: Optional[str] = None
    ttl: Optional[int] = None  # null/0 = 不过期


class SandboxInfo(BaseModel):
    """Sandbox 信息"""
    id: str
    status: SandboxStatus
    profile: str
    workspace_id: str
    capabilities: list[str]
    created_at: datetime
    expires_at: Optional[datetime]
    idle_expires_at: Optional[datetime]


class ExtendTTLRequest(BaseModel):
    """延长 TTL 请求"""
    extend_by: int = Field(..., ge=1, description="延长秒数")


# 能力调用相关
class PythonExecRequest(BaseModel):
    code: str
    timeout: int = Field(default=30, ge=1, le=300)


class PythonExecResult(BaseModel):
    success: bool
    output: str
    error: Optional[str] = None
    data: Optional[dict] = None  # 包含 execution_count, images 等


class ShellExecRequest(BaseModel):
    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: Optional[str] = None


class ShellExecResult(BaseModel):
    success: bool
    output: str
    error: Optional[str] = None
    exit_code: Optional[int] = None


class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    modified_at: Optional[datetime] = None
```

### 5.2 Workspace 模型

```python
class WorkspaceInfo(BaseModel):
    """Workspace 信息"""
    id: str
    managed: bool
    managed_by_sandbox_id: Optional[str]
    backend: str
    size_limit_mb: Optional[int]
    created_at: datetime
    last_accessed_at: datetime


class CreateWorkspaceRequest(BaseModel):
    """创建 Workspace 请求"""
    size_limit_mb: Optional[int] = None
```

## 6. 错误处理

### 6.1 异常层次

```python
class BayError(Exception):
    """Bay SDK 基础异常"""
    code: str
    message: str
    status_code: int
    details: dict


class NotFoundError(BayError):
    """资源不存在 (404)"""
    code = "not_found"
    status_code = 404


class UnauthorizedError(BayError):
    """未认证 (401)"""
    code = "unauthorized"
    status_code = 401


class ForbiddenError(BayError):
    """无权限 (403)"""
    code = "forbidden"
    status_code = 403


class ConflictError(BayError):
    """冲突 (409)"""
    code = "conflict"
    status_code = 409


class ValidationError(BayError):
    """参数校验错误 (400)"""
    code = "validation_error"
    status_code = 400


class SessionNotReadyError(BayError):
    """Session 未就绪 (503)"""
    code = "session_not_ready"
    status_code = 503


class TimeoutError(BayError):
    """操作超时 (504)"""
    code = "timeout"
    status_code = 504


class RuntimeError(BayError):
    """运行时错误 (502)"""
    code = "ship_error"
    status_code = 502


class SandboxExpiredError(ConflictError):
    """Sandbox 已过期"""
    code = "sandbox_expired"


class CapabilityNotSupportedError(ValidationError):
    """能力不支持"""
    code = "capability_not_supported"


class InvalidPathError(ValidationError):
    """无效路径"""
    code = "invalid_path"
```

### 6.2 错误映射

SDK 根据 HTTP 响应自动映射异常：

```python
ERROR_CODE_MAP = {
    "not_found": NotFoundError,
    "unauthorized": UnauthorizedError,
    "forbidden": ForbiddenError,
    "conflict": ConflictError,
    "validation_error": ValidationError,
    "session_not_ready": SessionNotReadyError,
    "timeout": TimeoutError,
    "ship_error": RuntimeError,
    "sandbox_expired": SandboxExpiredError,
    "capability_not_supported": CapabilityNotSupportedError,
    "invalid_path": InvalidPathError,
}
```

## 7. 使用示例

### 7.1 基本用法

```python
import asyncio
from bay import BayClient

async def main():
    # 创建客户端
    client = BayClient(
        endpoint_url="http://localhost:8000",
        access_token="your-token"
    )
    
    async with client:
        # 创建 Sandbox
        sandbox = await client.create_sandbox(
            profile="python-default",
            ttl=3600,  # 1 小时
        )
        
        print(f"Created sandbox: {sandbox.id}")
        print(f"Status: {sandbox.status}")
        
        # 执行 Python 代码
        result = await sandbox.python.exec("print('Hello, World!')")
        print(f"Output: {result.output}")
        
        # 文件操作
        await sandbox.filesystem.write_file("hello.txt", "Hello!")
        content = await sandbox.filesystem.read_file("hello.txt")
        print(f"File content: {content}")
        
        # Shell 命令
        result = await sandbox.shell.exec("ls -la")
        print(f"Files: {result.output}")
        
        # 保活（延长 idle timeout）
        await sandbox.keepalive()
        
        # 延长 TTL
        await sandbox.extend_ttl(1800)  # 再延长 30 分钟
        
        # 停止（回收算力，保留数据）
        await sandbox.stop()
        
        # 删除（彻底清理）
        await sandbox.delete()

asyncio.run(main())
```

### 7.2 使用 External Workspace

```python
async def use_external_workspace():
    async with BayClient(endpoint_url="...", access_token="...") as client:
        # 创建独立 Workspace
        workspace = await client.workspaces.create(size_limit_mb=2048)
        
        # 创建 Sandbox 绑定此 Workspace
        sandbox = await client.create_sandbox(
            profile="python-default",
            workspace_id=workspace.id,
            ttl=3600,
        )
        
        # 在 Sandbox 中写入数据
        await sandbox.filesystem.write_file("data.json", '{"key": "value"}')
        
        # 删除 Sandbox（Workspace 不会被删除）
        await sandbox.delete()
        
        # 创建新 Sandbox 复用同一 Workspace
        sandbox2 = await client.create_sandbox(
            profile="python-default",
            workspace_id=workspace.id,
        )
        
        # 数据仍然存在
        content = await sandbox2.filesystem.read_file("data.json")
        print(content)  # {"key": "value"}
```

### 7.3 错误处理

```python
from bay import BayClient
from bay.errors import (
    NotFoundError,
    SessionNotReadyError,
    SandboxExpiredError,
    ValidationError,
)

async def with_error_handling():
    async with BayClient(...) as client:
        try:
            sandbox = await client.get_sandbox("nonexistent-id")
        except NotFoundError:
            print("Sandbox not found")
        
        try:
            sandbox = await client.create_sandbox(ttl=3600)
            result = await sandbox.python.exec("1/0")
            if not result.success:
                print(f"Python error: {result.error}")
        except SessionNotReadyError as e:
            # Session 正在启动，可以重试
            print(f"Session starting, retry after {e.details.get('retry_after_ms')}ms")
        except SandboxExpiredError:
            print("Sandbox has expired")
```

### 7.4 幂等性支持

```python
async def idempotent_creation():
    async with BayClient(...) as client:
        # 使用幂等键确保创建操作可安全重试
        sandbox = await client.create_sandbox(
            profile="python-default",
            ttl=3600,
            idempotency_key="my-unique-key-123",
        )
        
        # 使用相同的 idempotency_key 再次调用会返回相同结果
        sandbox_again = await client.create_sandbox(
            profile="python-default",
            ttl=3600,
            idempotency_key="my-unique-key-123",
        )
        
        assert sandbox.id == sandbox_again.id
```

## 8. 配置选项

### 8.1 环境变量

| 变量名 | 说明 | 默认值 |
|:--|:--|:--|
| `BAY_ENDPOINT` | Bay API 地址 | 无（必填） |
| `BAY_TOKEN` | 访问令牌 | 无（必填） |
| `BAY_TIMEOUT` | 默认请求超时（秒） | 30 |
| `BAY_MAX_RETRIES` | 最大重试次数 | 3 |

### 8.2 客户端配置

```python
from bay import BayClient

client = BayClient(
    endpoint_url="http://localhost:8000",
    access_token="your-token",
    timeout=60.0,
    max_retries=5,
    retry_on_status=[503],  # 对 503 自动重试
)
```

## 9. 待敲定决策

### 9.1 同步 API 支持

| 选项 | 说明 | 倾向 |
|:--|:--|:--|
| A. 仅异步 | 只提供 `async/await` API | |
| B. 同步包装器 | 额外提供 `BayClientSync` | ✓ |
| C. 双重 API | 每个方法同时有同步和异步版本 | |

建议选 B，提供一个简单的同步包装器：

```python
from bay import BayClientSync

client = BayClientSync(endpoint_url="...", access_token="...")
sandbox = client.create_sandbox(ttl=3600)
result = sandbox.python.exec("print('hello')")
```

### 9.2 连接池

| 选项 | 说明 | 倾向 |
|:--|:--|:--|
| A. 每请求创建连接 | 简单但效率低 | |
| B. 共享 httpx.AsyncClient | 复用连接 | ✓ |

### 9.3 日志

| 选项 | 说明 | 倾向 |
|:--|:--|:--|
| A. 不记录 | SDK 不产生日志 | |
| B. 使用 logging | 标准库 logging | ✓ |
| C. 使用 structlog | 结构化日志 | |

### 9.4 包名

| 选项 | 说明 |
|:--|:--|
| A. `bay` | 简洁 |
| B. `bay-sdk` | 明确是 SDK |
| C. `shipyard-bay` | 保持 shipyard 前缀 |

## 10. 实现计划

### Phase 1: 核心功能
- [ ] 项目骨架（pyproject.toml, 目录结构）
- [ ] HTTP 客户端封装（基于 httpx）
- [ ] BayClient 主类
- [ ] Sandbox 资源类 + CRUD
- [ ] 能力调用（python, shell, filesystem）
- [ ] 错误处理
- [ ] 单元测试

### Phase 2: 增强功能
- [ ] Workspace 管理 API
- [ ] 同步包装器
- [ ] 幂等性支持
- [ ] 重试机制
- [ ] 类型导出（py.typed）

### Phase 3: 文档与发布
- [ ] README 文档
- [ ] API 文档（自动生成）
- [ ] 使用示例
- [ ] PyPI 发布

## 11. 附录：与旧 SDK 的迁移指南

### 11.1 导入变更

```python
# 旧
from shipyard import ShipyardClient, Spec, create_session_ship

# 新
from bay import BayClient
```

### 11.2 创建资源

```python
# 旧
client = ShipyardClient(endpoint_url="...", access_token="...")
ship = await client.create_ship(ttl=3600, spec=Spec(cpus=1.0, memory="512m"))

# 新
client = BayClient(endpoint_url="...", access_token="...")
sandbox = await client.create_sandbox(profile="python-default", ttl=3600)
```

### 11.3 能力调用

```python
# 旧
await ship.fs.read_file("path")
await ship.shell.exec("command")
await ship.python.exec("code")

# 新
await sandbox.filesystem.read_file("path")
await sandbox.shell.exec("command")
await sandbox.python.exec("code")
```

### 11.4 Session ID

旧 SDK 需要手动管理 `session_id`，新 SDK 完全内部化：

```python
# 旧
ship = await client.create_ship(ttl=3600, session_id="my-session-id")

# 新（无需 session_id）
sandbox = await client.create_sandbox(ttl=3600)
```

## 12. E2E 工作流场景覆盖分析

本节验证 SDK 设计是否能覆盖 [`plans/phase-1/e2e-workflow-scenarios.md`](../phase-1/e2e-workflow-scenarios.md) 中定义的所有场景。

### 场景覆盖表

| 场景 | 场景描述 | SDK 覆盖情况 | 说明 |
|:--|:--|:--|:--|
| **场景 1** | 交互式数据分析 | ✅ 完全覆盖 | `sandbox.filesystem.upload/download`, `sandbox.python.exec` 多轮执行, `sandbox.stop()` |
| **场景 2** | 脚本开发与调试 | ✅ 完全覆盖 | `sandbox.filesystem.write_file`, `sandbox.python.exec` 错误处理 |
| **场景 3** | 项目初始化与依赖安装 | ✅ 完全覆盖 | 嵌套目录创建, `sandbox.shell.exec` 运行 pip |
| **场景 4** | 简单快速执行 | ✅ 完全覆盖 | 最小路径：`create_sandbox` → `python.exec` → `delete` |
| **场景 5** | 长任务续命 | ✅ 完全覆盖 | `sandbox.extend_ttl()` + `idempotency_key` 支持 |
| **场景 6** | AI Agent 代码生成与迭代修复 | ✅ 完全覆盖 | 多轮执行、错误解析、TTL 续命、幂等保护 |
| **场景 7** | 路径安全与容器隔离 | ✅ 完全覆盖 | API 层路径校验返回 `InvalidPathError`；容器内执行是容器隔离层职责 |
| **场景 8** | Shell 驱动的 DevOps 自动化 | ✅ 完全覆盖 | `sandbox.shell.exec(command, cwd=...)` |
| **场景 9** | 超级无敌混合工作流 | ✅ 完全覆盖 | 所有能力组合 |
| **场景 10** | GC 混沌长工作流 | ✅ 完全覆盖 | `keepalive`, `extend_ttl`, `stop`, 异常处理 |

### 详细场景 SDK 用法示例

#### 场景 1：交互式数据分析

```python
async def interactive_data_analysis():
    async with BayClient(...) as client:
        sandbox = await client.create_sandbox(ttl=3600)
        
        # 上传数据文件
        with open("sales.csv", "rb") as f:
            await sandbox.filesystem.upload("sales.csv", f.read())
        
        # 多轮 Python 执行（变量在同一 Session 内保持）
        await sandbox.python.exec("import pandas as pd; df = pd.read_csv('sales.csv')")
        result = await sandbox.python.exec("df['revenue'].sum()")
        print(result.output)
        
        # 生成图表并下载
        await sandbox.python.exec("""
import matplotlib.pyplot as plt
df['revenue'].plot()
plt.savefig('chart.png')
""")
        chart_data = await sandbox.filesystem.download("chart.png")
        with open("local_chart.png", "wb") as f:
            f.write(chart_data)
        
        # 停止（释放算力，保留数据）
        await sandbox.stop()
        
        # 恢复执行（变量丢失，文件保留）
        result = await sandbox.python.exec("df.head()")  # NameError
        assert not result.success  # 变量丢失
        
        result = await sandbox.python.exec("import pandas as pd; pd.read_csv('sales.csv').head()")
        assert result.success  # 文件仍存在
```

#### 场景 5：长任务续命

```python
async def long_running_task():
    async with BayClient(...) as client:
        sandbox = await client.create_sandbox(ttl=120)
        
        # 执行长任务
        result = await sandbox.python.exec("""
import time
print('start')
time.sleep(5)
print('done')
""", timeout=60)
        
        # 接近过期前续命（带幂等键）
        await sandbox.extend_ttl(600, idempotency_key="extend-001")
        
        # 网络重试（相同幂等键，不会多续）
        await sandbox.extend_ttl(600, idempotency_key="extend-001")
        
        # 继续执行
        await sandbox.python.exec("print('still alive')")
```

#### 场景 6：AI Agent 代码生成与迭代修复

```python
async def agent_coding_workflow():
    async with BayClient(...) as client:
        sandbox = await client.create_sandbox(
            ttl=300,
            idempotency_key="agent-task-001-create"
        )
        
        # Agent 生成代码（有 Bug）
        await sandbox.filesystem.write_file("solution.py", """
def calculate_fibonacci(n):
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonaci(n-2)  # typo

print(calculate_fibonacci(10))
""")
        
        # 执行失败
        result = await sandbox.python.exec("exec(open('solution.py').read())")
        if not result.success:
            # Agent 解析错误
            error = result.error  # "NameError: name 'calculate_fibonaci' is not defined"
            # Agent 修复代码...
            
        # Agent 修复后重新写入
        await sandbox.filesystem.write_file("solution.py", """
def calculate_fibonacci(n):
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)

print(calculate_fibonacci(10))
""")
        
        # 执行成功
        result = await sandbox.python.exec("exec(open('solution.py').read())")
        assert result.success
        assert "55" in result.output
        
        # TTL 续命
        await sandbox.extend_ttl(600, idempotency_key="agent-task-001-extend-1")
```

#### 场景 8：Shell 驱动的 DevOps

```python
async def devops_workflow():
    async with BayClient(...) as client:
        sandbox = await client.create_sandbox()
        
        # 验证工具可用
        result = await sandbox.shell.exec("git --version && node --version")
        assert result.success
        
        # 创建 Node.js 项目
        await sandbox.filesystem.write_file("myapp/package.json", """{
  "name": "myapp",
  "scripts": {
    "build": "echo 'Building...'",
    "test": "echo 'Testing...'"
  }
}""")
        
        # 在项目目录运行构建
        result = await sandbox.shell.exec("npm run build", cwd="myapp")
        assert result.success
        
        # Git 工作流
        await sandbox.shell.exec("git init myrepo")
        await sandbox.shell.exec(
            "git config user.email 'test@example.com' && git config user.name 'Test'",
            cwd="myrepo"
        )
        
        # 打包下载
        await sandbox.shell.exec("tar -czvf myapp.tar.gz myapp/")
        tarball = await sandbox.filesystem.download("myapp.tar.gz")
```

### 需要注意的边界情况

1. **停止后自动恢复**：`sandbox.stop()` 后再调用任何能力方法（如 `python.exec`），Bay 会自动 `ensure_running` 创建新 Session

2. **变量丢失 vs 文件保留**：`stop()` 后 Python 变量丢失（新 Kernel），但 Workspace 文件保留

3. **TTL 过期不可复活**：一旦 `expires_at < now`，`extend_ttl()` 会抛出 `SandboxExpiredError`

4. **路径校验在 SDK 层透传**：SDK 不做额外路径校验，依赖 Bay API 返回 `InvalidPathError`
