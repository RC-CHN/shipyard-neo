# Shipyard Neo Python SDK

Bay API 的 Python 客户端库 - 为 AI 代理提供安全的沙箱执行环境。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

## 特性

- **异步优先设计** - 基于 `httpx` 构建，支持现代异步 Python
- **类型安全** - 使用 Pydantic 模型提供完整的类型提示
- **基于能力的访问控制** - 支持 Python、Shell 和文件系统操作
- **自动会话管理** - 延迟启动，透明恢复
- **幂等性支持** - 网络故障时可安全重试
- **持久化存储** - Cargo 卷在沙箱重启后仍然保留

## 安装

```bash
pip install shipyard-neo-sdk
```

或从源码安装：

```bash
cd shipyard-neo-sdk
pip install -e .
```

## 快速开始

```python
import asyncio
from shipyard_neo import BayClient

async def main():
    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="your-token",
    ) as client:
        # 创建沙箱
        sandbox = await client.create_sandbox(profile="python-default", ttl=600)
        
        # 执行 Python 代码
        result = await sandbox.python.exec("print('Hello, World!')")
        print(result.output)  # "Hello, World!\n"
        
        # 执行 shell 命令
        result = await sandbox.shell.exec("ls -la")
        print(result.output)
        
        # 文件操作
        await sandbox.filesystem.write_file("app.py", "print('hi')")
        content = await sandbox.filesystem.read_file("app.py")
        
        # 清理
        await sandbox.delete()

asyncio.run(main())
```

## API 参考

### BayClient

SDK 的主入口。

```python
from shipyard_neo import BayClient

# 使用环境变量 (BAY_ENDPOINT, BAY_TOKEN)
async with BayClient() as client:
    ...

# 显式配置
async with BayClient(
    endpoint_url="http://localhost:8000",
    access_token="your-token",
    timeout=30.0,  # 默认请求超时时间
    max_retries=3,  # 最大重试次数
) as client:
    ...
```

#### 方法

| 方法 | 描述 |
|:--|:--|
| `create_sandbox()` | 创建新沙箱 |
| `get_sandbox(id)` | 获取已存在的沙箱 |
| `list_sandboxes()` | 列出所有沙箱 |
| `list_profiles()` | 列出可用的运行时 Profile |
| `cargos` | 访问 CargoManager 进行 cargo 操作 |
| `skills` | 访问 SkillManager 进行技能生命周期管理 |

### 创建沙箱

```python
# 基本创建
sandbox = await client.create_sandbox(
    profile="python-default",  # Profile ID（默认值: "python-default"）
    ttl=600,                   # 生存时间，单位秒（可选）
)

# 使用外部 cargo
cargo = await client.cargos.create(size_limit_mb=512)
sandbox = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,  # 附加外部 cargo
    ttl=600,
)

# 使用幂等性键（用于安全重试）
sandbox = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="unique-request-id-123",
)
```

### 沙箱属性

```python
sandbox.id            # str: 唯一沙箱 ID
sandbox.status        # SandboxStatus: IDLE, STARTING, READY, FAILED, EXPIRED
sandbox.profile       # str: Profile ID
sandbox.cargo_id      # str: 关联的 cargo ID
sandbox.capabilities  # list[str]: ["python", "shell", "filesystem"]
sandbox.created_at    # datetime: 创建时间戳
sandbox.expires_at    # datetime | None: TTL 过期时间（None 表示无限期）
```

### 沙箱生命周期

```python
# 从服务器刷新本地状态
await sandbox.refresh()

# 停止沙箱（回收计算资源，保留文件）
await sandbox.stop()

# 永久删除沙箱
await sandbox.delete()

# 延长 TTL N 秒
await sandbox.extend_ttl(300)  # 延长 5 分钟

# 使用幂等性键
await sandbox.extend_ttl(300, idempotency_key="extend-001")

# 发送保活信号（仅延长空闲超时，不延长 TTL）
await sandbox.keepalive()
```

### 列出沙箱

```python
from shipyard_neo import SandboxStatus

# 列出所有沙箱
result = await client.list_sandboxes()
for sb in result.items:
    print(f"{sb.id}: {sb.status}")

# 使用分页
result = await client.list_sandboxes(limit=50)
while result.next_cursor:
    result = await client.list_sandboxes(cursor=result.next_cursor, limit=50)
    for sb in result.items:
        process(sb)

# 按状态过滤
result = await client.list_sandboxes(status=SandboxStatus.READY)
```

### 列出 Profiles

```python
# 获取可用 Profile 列表
profiles = await client.list_profiles(detail=True)
for p in profiles.items:
    print(p.id, p.capabilities)
```

## 能力

### Python 能力

在 IPython 内核中执行 Python 代码。变量在多次调用间保持持久。

```python
# 简单执行
result = await sandbox.python.exec("print('Hello!')")
assert result.success
print(result.output)  # "Hello!\n"

# 多行代码
code = """
def fibonacci(n):
    if n <= 1: return n
    return fibonacci(n-1) + fibonacci(n-2)

result = fibonacci(10)
print(f"fib(10) = {result}")
"""
result = await sandbox.python.exec(code)

# 变量持久化
await sandbox.python.exec("x = 42")
result = await sandbox.python.exec("print(x)")  # 可以正常工作！

# 错误处理
result = await sandbox.python.exec("1 / 0")
if not result.success:
    print(result.error)  # "ZeroDivisionError: division by zero"

# 自定义超时
result = await sandbox.python.exec("import time; time.sleep(10)", timeout=15)
```

#### PythonExecResult

| 属性 | 类型 | 描述 |
|:--|:--|:--|
| `success` | `bool` | 执行是否无异常完成 |
| `output` | `str` | stdout 输出 |
| `error` | `str \| None` | 错误堆栈跟踪（失败时） |
| `data` | `dict \| None` | IPython 富文本输出 |

**成功示例：**

```python
result = await sandbox.python.exec("2 ** 10")
# result.success = True
# result.output = "1024\n"
# result.error = None
# result.data = {
#     'execution_count': 2,
#     'output': {'text': '1024', 'images': []}
# }
```

**失败示例：**

```python
result = await sandbox.python.exec("1 / 0")
# result.success = False
# result.output = ""
# result.error = """
# ---------------------------------------------------------------------------
# ZeroDivisionError                         Traceback (most recent call last)
# Cell In[4], line 1
# ----> 1 1 / 0
#
# ZeroDivisionError: division by zero
# """
# result.data = None
```

### Shell 能力

执行 shell 命令。

```python
# 简单命令
result = await sandbox.shell.exec("echo 'Hello!'")
print(result.output)      # "Hello!\n"
print(result.exit_code)   # 0

# 管道操作
result = await sandbox.shell.exec("ls -la | grep py")

# 自定义工作目录
result = await sandbox.shell.exec("pwd && ls", cwd="src")

# 退出码处理
result = await sandbox.shell.exec("exit 42")
assert not result.success
assert result.exit_code == 42

# 自定义超时
result = await sandbox.shell.exec("sleep 10", timeout=15)
```

#### ShellExecResult

| 属性 | 类型 | 描述 |
|:--|:--|:--|
| `success` | `bool` | 执行是否成功 (exit_code == 0) |
| `output` | `str` | 合并的 stdout + stderr 输出 |
| `error` | `str \| None` | 错误消息 |
| `exit_code` | `int \| None` | 进程退出码 |

**成功示例：**

```python
result = await sandbox.shell.exec("whoami && pwd")
# result.success = True
# result.output = "shipyard\n/workspace\n"
# result.error = None
# result.exit_code = 0
```

**失败示例：**

```python
result = await sandbox.shell.exec("exit 42")
# result.success = False
# result.output = ""
# result.error = None
# result.exit_code = 42
```

**管道示例：**

```python
result = await sandbox.shell.exec("echo -e 'apple\\nbanana\\ncherry' | grep an")
# result.success = True
# result.output = "banana\n"
# result.exit_code = 0
```

### Filesystem 能力

在沙箱工作区（`/workspace`）中读取、写入和管理文件。

```python
# 写入文本文件
await sandbox.filesystem.write_file("app.py", "print('hello')")

# 写入嵌套路径（目录自动创建）
await sandbox.filesystem.write_file("src/main.py", "# main code")

# 读取文本文件
content = await sandbox.filesystem.read_file("app.py")

# 列出目录
entries = await sandbox.filesystem.list_dir(".")
for entry in entries:
    print(f"{entry.name}: {'dir' if entry.is_dir else 'file'}")

# 列出嵌套目录
entries = await sandbox.filesystem.list_dir("src")

# 删除文件或目录
await sandbox.filesystem.delete("app.py")

# 上传二进制文件
binary_data = open("image.png", "rb").read()
await sandbox.filesystem.upload("assets/image.png", binary_data)

# 下载二进制文件
data = await sandbox.filesystem.download("assets/image.png")
open("downloaded.png", "wb").write(data)
```

相对路径仍以 `/workspace` 为根，也可以显式传递允许的绝对路径，例如
`/workspace/output.txt` 或 `/tmp/output.txt`。`/tmp` 只属于当前 Ship 容器，
容器重启后不保证保留，也不会在容器间共享；跨容器交换和需要持久化的文件请使用
`/workspace`。

### Browser 能力

在沙箱中执行浏览器自动化命令（需要 Profile 支持 `browser` 能力）。

```python
# 单条命令
result = await sandbox.browser.exec("open https://example.com")
print(result.success, result.output)

# 批量命令（用于不需要中间决策的确定性流程）
batch = await sandbox.browser.exec_batch(
    [
        "open https://example.com",
        "wait --load networkidle",
        "snapshot -i",
    ],
    timeout=120,
)
print(batch.success, batch.completed_steps, batch.total_steps)
```

#### FileInfo

| 属性 | 类型 | 描述 |
|:--|:--|:--|
| `name` | `str` | 文件/目录名称 |
| `path` | `str` | 相对于 /workspace 的完整路径 |
| `is_dir` | `bool` | 是否为目录 |
| `size` | `int \| None` | 大小（字节）（目录为 None） |
| `modified_at` | `datetime \| None` | 最后修改时间 |

**目录列表示例：**

```python
entries = await sandbox.filesystem.list_dir(".")
# 返回: [FileInfo, FileInfo, ...]
#
# 目录示例：
#   entry.name = "mydir"
#   entry.path = "mydir"
#   entry.is_dir = True
#   entry.size = None
#
# 文件示例：
#   entry.name = "test.txt"
#   entry.path = "test.txt"
#   entry.is_dir = False
#   entry.size = 13

# 打印所有条目
for e in entries:
    if e.is_dir:
        print(f"📁 {e.name}/")
    else:
        print(f"📄 {e.name} ({e.size} bytes)")

# 典型输出：
# 📁 mydir/
# 📁 nested/
# 📄 test.txt (13 bytes)
```

## Cargo 管理

Cargo 是持久化存储，在沙箱重启后仍然保留。有两种类型：
- **托管 cargo**：随沙箱自动创建，随沙箱删除
- **外部 cargo**：单独创建，沙箱删除后仍然保留

```python
# 创建外部 cargo
cargo = await client.cargos.create(size_limit_mb=512)
print(f"Created cargo: {cargo.id}")

# 使用幂等性键创建（用于安全重试）
cargo = await client.cargos.create(
    size_limit_mb=512,
    idempotency_key="cargo-unique-123",
)

# 获取 cargo 信息
cargo = await client.cargos.get(cargo.id)
print(f"Managed: {cargo.managed}")
print(f"Size limit: {cargo.size_limit_mb} MB")

# 列出外部 cargo（默认不包含托管 cargo）
result = await client.cargos.list()
for c in result.items:
    print(f"{c.id}: {c.size_limit_mb} MB")

# 删除 cargo
await client.cargos.delete(cargo.id)
```

### 使用外部 Cargo

```python
# 创建外部 cargo
cargo = await client.cargos.create(size_limit_mb=1024)

# 使用外部 cargo 创建沙箱
sandbox = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,
    ttl=600,
)

# 向 cargo 写入数据
await sandbox.filesystem.write_file("data.txt", "Important data")

# 删除沙箱（cargo 保留！）
await sandbox.delete()

# 使用同一 cargo 创建新沙箱
sandbox2 = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,
    ttl=600,
)

# 数据仍然存在！
content = await sandbox2.filesystem.read_file("data.txt")
assert content == "Important data"

# 清理
await sandbox2.delete()
await client.cargos.delete(cargo.id)
```

## Skill 生命周期管理（client.skills）

```python
from shipyard_neo import SkillReleaseStage

# 1) 收集执行证据
py = await sandbox.python.exec("print('step1')", tags="etl")
sh = await sandbox.shell.exec("echo step2", tags="etl")

# 2) 创建候选
candidate = await client.skills.create_candidate(
    skill_key="etl-loader",
    source_execution_ids=[py.execution_id, sh.execution_id],
    scenario_key="csv-import",
)

# 3) 评估
_ = await client.skills.evaluate_candidate(
    candidate.id,
    passed=True,
    score=0.96,
    report="ok",
)

# 4) 晋升发布
release = await client.skills.promote_candidate(candidate.id, stage=SkillReleaseStage.CANARY)

# 5) 查询 / 回滚
_ = await client.skills.list_releases(skill_key="etl-loader", active_only=True)
_ = await client.skills.rollback_release(release.id)
```

## 错误处理

所有错误都继承自 `BayError`。

```python
from shipyard_neo import (
    BayError,
    NotFoundError,
    UnauthorizedError,
    ForbiddenError,
    QuotaExceededError,
    ConflictError,
    ValidationError,
    SessionNotReadyError,
    RequestTimeoutError,
    ShipError,
    SandboxExpiredError,
    SandboxTTLInfiniteError,
    CapabilityNotSupportedError,
    InvalidPathError,
    CargoFileNotFoundError,
)

try:
    sandbox = await client.get_sandbox("nonexistent-id")
except NotFoundError as e:
    print(f"Sandbox not found: {e.message}")
except BayError as e:
    print(f"API error: {e.message}")
```

### 错误类型

| 异常 | HTTP 状态码 | 描述 |
|:--|:--|:--|
| `UnauthorizedError` | 401 | 访问令牌无效或缺失 |
| `ForbiddenError` | 403 | 权限被拒绝 |
| `NotFoundError` | 404 | 资源未找到 |
| `QuotaExceededError` | 429 | 超出速率限制或配额 |
| `ConflictError` | 409 | 资源冲突（如 cargo 正在使用中） |
| `ValidationError` | 400 | 请求参数无效 |
| `SessionNotReadyError` | 503 | 会话未就绪（请重试） |
| `RequestTimeoutError` | 504 | 请求超时 |
| `ShipError` | 502 | Ship（容器）错误 |
| `SandboxExpiredError` | 409 | 沙箱 TTL 已过期 |
| `SandboxTTLInfiniteError` | 409 | 无法延长无限期 TTL |
| `CapabilityNotSupportedError` | 400 | Profile 不支持该能力 |
| `InvalidPathError` | 400 | 文件路径无效 |
| `CargoFileNotFoundError` | 404 | 工作区中未找到文件 |

## 幂等性

为了在网络故障时安全重试，请使用幂等性键：

```python
# 创建沙箱
sandbox = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="unique-request-123",
)

# 使用相同的键重试会返回相同的沙箱
sandbox2 = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="unique-request-123",
)
assert sandbox.id == sandbox2.id

# TTL 延长
await sandbox.extend_ttl(300, idempotency_key="extend-001")
# 重试返回相同结果
await sandbox.extend_ttl(300, idempotency_key="extend-001")
```

## 环境变量

SDK 支持通过环境变量进行配置：

| 变量 | 描述 |
|:--|:--|
| `BAY_ENDPOINT` | Bay API 端点 URL |
| `BAY_TOKEN` | 认证令牌 |
| `BAY_TIMEOUT` | 默认请求超时时间（秒） |
| `BAY_MAX_RETRIES` | 最大重试次数 |

```python
import os

os.environ["BAY_ENDPOINT"] = "http://localhost:8000"
os.environ["BAY_TOKEN"] = "your-token"

# 无需显式配置
async with BayClient() as client:
    sandbox = await client.create_sandbox()
```

## 高级用法

### 会话生命周期

会话被透明管理。能力调用（Python/Shell/Filesystem）会在需要时自动启动会话：

```python
# 沙箱以 IDLE 状态启动（无会话）
sandbox = await client.create_sandbox()
print(sandbox.status)  # SandboxStatus.IDLE

# 首次能力调用触发会话启动
result = await sandbox.python.exec("print('hello')")
await sandbox.refresh()
print(sandbox.status)  # SandboxStatus.READY

# 停止会回收资源但保留文件
await sandbox.stop()
print(sandbox.status)  # SandboxStatus.IDLE

# 下次能力调用会自动恢复
result = await sandbox.python.exec("print('back!')")
# 注意：停止后 Python 变量会丢失
```

### 长时间运行的任务

对于长时间运行的操作，请延长超时时间：

```python
# 使用更长超时的 Python 执行
result = await sandbox.python.exec(
    "import time; time.sleep(60)",
    timeout=120,
)

# 使用更长超时的 Shell 执行
result = await sandbox.shell.exec(
    "find / -name '*.py' 2>/dev/null",
    timeout=120,
)
```

### 空闲超时的保活

如果你在操作之间但希望防止空闲超时：

```python
# 保活会延长空闲超时但不会延长 TTL
await sandbox.keepalive()

# 注意：如果没有会话存在，保活不会启动会话
```

## 许可证

AGPL-3.0-or-later。详见 [LICENSE](./LICENSE)。
