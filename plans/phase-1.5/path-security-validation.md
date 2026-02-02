# 路径安全校验设计

> 更新日期: 2026-02-02
>
> 状态: ✅ 已实现

## 1. 背景与动机

Shipyard 需要确保用户提供的文件路径不会逃逸 cargo 边界。当前 Ship 端已有校验逻辑，但 Bay 端缺乏前置验证，导致：

1. **资源浪费**：恶意或错误路径请求需到达 Ship 才被拒绝
2. **错误信息泄露**：Ship 返回的 403 可能包含内部路径信息
3. **攻击面暴露**：Bay 作为入口应该是第一道防线

## 2. 容器隔离与卷挂载分析

### 2.1 架构概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                             宿主机                                        │
│                                                                          │
│   ┌──────────────────┐                 ┌─────────────────────────────┐  │
│   │       Bay        │                 │      Docker Volume          │  │
│   │   (API Gateway)  │                 │  bay-cargo-ws-xxxx      │  │
│   └────────┬─────────┘                 └─────────────┬───────────────┘  │
│            │ HTTP                                    │                   │
│            ▼                                         │ mount             │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │                        Ship 容器                                 │   │
│   │   ┌─────────────────────────────────────────────────────────┐  │   │
│   │   │                    /workspace                            │  │   │
│   │   │   (Docker Volume 挂载点，用户可读写区域)                   │◀─┘   │
│   │   └─────────────────────────────────────────────────────────┘      │
│   │                                                                     │
│   │   ┌─────────────────────────────────────────────────────────┐      │
│   │   │  /app (Ship 代码)  /etc  /usr  ... (容器其他目录)        │      │
│   │   │   (只读镜像层，用户无法通过 API 访问)                      │      │
│   │   └─────────────────────────────────────────────────────────┘      │
│   └────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 卷挂载机制

**Bay 侧 - DockerDriver**

文件: `pkgs/bay/app/drivers/docker/docker.py:34, 195`

```python
# 固定挂载路径 (常量)
WORKSPACE_MOUNT_PATH = "/workspace"

# 容器创建时挂载 Docker Volume
host_config: dict = {
    "Binds": [f"{cargo.driver_ref}:{WORKSPACE_MOUNT_PATH}:rw"],
    ...
}
```

**Cargo 模型**

文件: `pkgs/bay/app/models/cargo.py:1`

```python
backend: str = Field(default="docker_volume")  # docker_volume | k8s_pvc
driver_ref: str = Field(default="")  # Volume name: "bay-cargo-ws-xxxx"

@property
def mount_path(self) -> str:
    """Container mount path - always /workspace."""
    return "/workspace"
```

**CargoManager 创建 Volume**

文件: `pkgs/bay/app/managers/cargo/cargo.py:1`

```python
volume_name = f"bay-cargo-{cargo_id}"
await self._driver.create_volume(
    name=volume_name,
    labels={"bay.owner": owner, "bay.cargo_id": cargo_id, ...},
)
```

### 2.3 Ship 容器目录结构

文件: `pkgs/ship/Dockerfile:61-65`

```dockerfile
# 创建 shipyard 用户，home 目录设为 /workspace
useradd -u 1000 -g shipyard -d /workspace -s /bin/bash shipyard
mkdir -p /workspace
chown shipyard:shipyard /workspace
chmod 755 /workspace
```

容器内目录:
```
/
├── app/                    # Ship 应用代码 (来自镜像)
├── workspace/               # Docker Volume 挂载点 (用户数据)
│   └── (用户文件...)
├── etc/                    # 系统配置 (来自镜像)
├── usr/                    # 系统程序 (来自镜像)
└── ...
```

### 2.4 安全边界

| 层级 | 保护机制 | 说明 |
|-----|---------|------|
| **Docker 隔离** | 容器 namespace | 进程、网络、文件系统隔离 |
| **Volume 挂载** | 仅 `/workspace` | 用户只能读写 Volume 内容 |
| **Ship API** | `resolve_path()` | 所有路径操作限制在 `/workspace` 内 |
| **用户权限** | shipyard (UID 1000) | 命令以非 root 用户执行 |

**关键点**: 即使 Bay 不做路径校验，Ship 容器内的攻击面也受限于:
1. Volume 仅挂载 `/workspace`，无法访问宿主机其他目录
2. Ship API 的 `resolve_path()` 拒绝逃逸到容器内其他目录
3. 容器内敏感目录 (`/app`, `/etc`) 来自只读镜像层

**但 Bay 前置校验仍有价值**:
1. 快速拒绝明显恶意请求
2. 减少 Ship 容器负载
3. 更友好的错误消息 (400 vs 403)
4. 防御纵深原则

## 3. 现有实现分析

### 3.1 Ship 端 (已实现)

#### 2.1.1 filesystem 组件 - `resolve_path()`

**文件**: `pkgs/ship/app/workspace.py:26-54`

```python
def resolve_path(path: str) -> Path:
    workspace_dir = get_workspace_dir().resolve()  # /workspace
    candidate = Path(path)

    if not candidate.is_absolute():
        candidate = workspace_dir / candidate

    candidate = candidate.resolve()  # 解析 symlink 和 ..
    try:
        candidate.relative_to(workspace_dir)  # 校验边界
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: path must be within cargo {workspace_dir}",
        )
    return candidate
```

**调用点**:
- `filesystem.py:74` - `create_file()`
- `filesystem.py:99` - `read_file()`
- `filesystem.py:136` - `write_file()`
- `filesystem.py:162` - `edit_file()`
- `filesystem.py:224` - `delete_file()`
- `filesystem.py:254` - `list_dir()`
- `filesystem.py:318` - `upload_file()`
- `filesystem.py:348` - `download_file()`

**特点**:
- ✅ 接受相对路径和绝对路径
- ✅ 使用 `.resolve()` 解析符号链接和 `..`
- ✅ 使用 `.relative_to()` 验证边界
- ✅ 统一拒绝逃逸访问返回 403

#### 2.1.2 shell 组件 - `run_command()` 中的 `cwd` 处理

**文件**: `pkgs/ship/app/components/user_manager.py:206-220`

```python
working_dir = WORKSPACE_ROOT
if cwd:
    if not os.path.isabs(cwd):
        working_dir = working_dir / cwd
    else:
        working_dir = Path(cwd)
    # resolve working dir
    working_dir = working_dir.resolve()
    try:
        working_dir.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: path must be within cargo: {WORKSPACE_ROOT}",
        )
```

**特点**:
- ✅ 与 `resolve_path()` 逻辑一致
- ✅ 同样使用 `.resolve()` + `.relative_to()` 模式

### 2.2 Bay 端 (未实现)

#### 2.2.1 API 层 - `capabilities.py`

**文件**: `pkgs/bay/app/api/v1/capabilities.py`

路径直接作为参数传递，无校验:

```python
# GET /{sandbox_id}/filesystem/files
path: str = Query(..., description="File path relative to /workspace")

# PUT /{sandbox_id}/filesystem/files
class FileWriteRequest(BaseModel):
    path: str  # Relative to /workspace

# POST /{sandbox_id}/shell/exec
class ShellExecRequest(BaseModel):
    cwd: str | None = None  # Relative to /workspace
```

#### 2.2.2 CapabilityRouter

**文件**: `pkgs/bay/app/router/capability/capability.py`

路径直接传递给 adapter，无校验:

```python
async def read_file(self, sandbox: Sandbox, path: str) -> str:
    # path 未校验
    return await adapter.read_file(path)
```

#### 2.2.3 ShipAdapter

**文件**: `pkgs/bay/app/adapters/ship.py`

路径直接放入 HTTP 请求体:

```python
async def read_file(self, path: str) -> str:
    result = await self._post("/fs/read_file", {"path": path})
    return result.get("content", "")
```

## 3. 威胁分析

### 3.1 攻击向量

| 输入示例 | 意图 | Ship 行为 | 期望 Bay 行为 |
|---------|------|----------|--------------|
| `../../../etc/passwd` | 目录穿越 | 403 拒绝 | 400 快速拒绝 |
| `/etc/passwd` | 绝对路径 | 403 拒绝 | 400 快速拒绝 |
| `subdir/../file.txt` | 隐蔽穿越 | ✅ 正常处理 | ✅ 放行 (见3.2) |
| `valid/path.txt` | 正常路径 | ✅ 正常处理 | ✅ 放行 |
| `symlink_to_outside` | 符号链接逃逸 | 403 拒绝 | 放行 (无法检测) |
| `....//....//etc/passwd` | 编码绕过 | 403 拒绝 | 400 快速拒绝 |

### 4.2 决策点: `subdir/../file.txt` 的处理

**选项 A**: Bay 拒绝任何包含 `..` 的路径
- 优点: 简单、安全
- 缺点: 可能拒绝合法请求 (如 `./subdir/../README.md`)

**选项 B**: Bay 做语法检查 + 路径规范化，允许规范化后不逃逸的路径 ✅ 推荐
- 优点: 与 Ship 行为一致，用户体验更好
- 缺点: Bay 需要实现路径规范化逻辑（但逻辑简单）

**选项 C**: Bay 禁止 `..` 作为路径组件的开头 (如 `../foo`), 但允许中间的 `..`
- 中间方案，但规则不直观

**推荐**: 选项 B - Bay 做语法检查 + 路径规范化

理由:
1. **容器隔离已提供强保护** - 即使恶意路径到达 Ship，也只能访问 `/workspace` 内容
2. **用户友好** - 一些 SDK/工具可能生成包含 `..` 的合法路径
3. **与 Ship 一致** - 减少 Bay/Ship 行为差异导致的困惑
4. **规范化逻辑简单** - 使用 `PurePosixPath` 即可实现

## 4. 设计方案

### 4.1 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                           Bay                                    │
│  ┌─────────────┐    ┌─────────────────┐    ┌─────────────────┐ │
│  │   API 层    │───▶│  PathValidator  │───▶│ CapabilityRouter│ │
│  │ capabilities│    │   (新增)         │    │                 │ │
│  └─────────────┘    └─────────────────┘    └─────────────────┘ │
│                            │                        │           │
│                      400 BadRequest           ShipAdapter       │
│                      (快速失败)                     │           │
└─────────────────────────────────────────────────────│───────────┘
                                                      │
                                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                           Ship                                   │
│  ┌─────────────┐    ┌─────────────────┐    ┌─────────────────┐ │
│  │   API 层    │───▶│  resolve_path   │───▶│   实际操作      │ │
│  │ filesystem  │    │   (已实现)       │    │ (read/write/...)│ │
│  └─────────────┘    └─────────────────┘    └─────────────────┘ │
│                            │                                    │
│                      403 Forbidden                              │
│                      (最终防线)                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 Bay 侧实现

#### 5.2.1 新增模块 `pkgs/bay/app/validators/path.py`

```python
"""Path validation utilities for Bay API.

Bay performs syntactic validation with path normalization.
Ship performs full semantic validation (resolve symlinks, etc.).

Design: 选项 B - 允许规范化后不逃逸的路径
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.errors import InvalidPathError


def validate_relative_path(path: str, *, field_name: str = "path") -> str:
    """Validate and normalize path to ensure it stays within cargo.

    Rules:
    1. Must not be empty
    2. Must not be absolute (start with /)
    3. Must not contain null bytes
    4. After normalization, must not escape cargo (start with ..)

    Args:
        path: Path to validate
        field_name: Name of field for error messages

    Returns:
        The normalized path if valid

    Raises:
        InvalidPathError: If validation fails

    Examples:
        >>> validate_relative_path("file.txt")
        'file.txt'
        >>> validate_relative_path("subdir/../file.txt")
        'file.txt'  # normalized
        >>> validate_relative_path("./a/b/../c.txt")
        'a/c.txt'  # normalized
        >>> validate_relative_path("../file.txt")
        InvalidPathError  # escapes cargo
    """
    if not path:
        raise InvalidPathError(
            message=f"{field_name} cannot be empty",
            details={"field": field_name, "reason": "empty_path"},
        )

    # Check for null bytes (injection attack)
    if "\x00" in path:
        raise InvalidPathError(
            message=f"{field_name} contains invalid characters",
            details={"field": field_name, "reason": "null_byte"},
        )

    p = PurePosixPath(path)

    # Check absolute path
    if p.is_absolute():
        raise InvalidPathError(
            message=f"{field_name} must be a relative path",
            details={"field": field_name, "reason": "absolute_path"},
        )

    # Normalize path: resolve . and .. components
    # PurePosixPath doesn't have resolve(), so we manually normalize
    parts: list[str] = []
    for part in p.parts:
        if part == ".":
            continue
        elif part == "..":
            if parts:
                parts.pop()
            else:
                # Trying to go above cargo root
                raise InvalidPathError(
                    message=f"{field_name} escapes cargo boundary",
                    details={"field": field_name, "reason": "path_traversal"},
                )
        else:
            parts.append(part)

    # Return normalized path
    if not parts:
        return "."
    return "/".join(parts)
```

#### 4.2.2 新增错误类型 `pkgs/bay/app/errors.py`

```python
class InvalidPathError(BayError):
    """Invalid file path (absolute, traversal, etc.)."""

    code = "invalid_path"
    message = "Invalid path"
    status_code = 400
```

#### 4.2.3 API 层集成

**方式 A: 使用 Pydantic 验证器 (推荐)**

```python
from pydantic import field_validator
from app.validators.path import validate_relative_path

class FileWriteRequest(BaseModel):
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return validate_relative_path(v)
```

**方式 B: 使用依赖注入**

```python
from fastapi import Depends

def validated_path(path: str = Query(...)) -> str:
    return validate_relative_path(path)

@router.get("/{sandbox_id}/filesystem/files")
async def read_file(
    path: str = Depends(validated_path),
    ...
):
    ...
```

### 4.3 影响范围

需要添加路径校验的端点:

| 端点 | 路径字段 | 校验方式 |
|------|---------|---------|
| `GET /{id}/filesystem/files` | `path` (Query) | 依赖注入 |
| `PUT /{id}/filesystem/files` | `path` (Body) | Pydantic 验证器 |
| `DELETE /{id}/filesystem/files` | `path` (Query) | 依赖注入 |
| `GET /{id}/filesystem/directories` | `path` (Query) | 依赖注入 |
| `POST /{id}/filesystem/upload` | `path` (Form) | 手动调用 |
| `GET /{id}/filesystem/download` | `path` (Query) | 依赖注入 |
| `POST /{id}/shell/exec` | `cwd` (Body, optional) | Pydantic 验证器 |

## 6. 典型输入行为对照表

| 输入 | Bay 校验结果 | 规范化后 | Ship 校验 | 最终结果 |
|------|------------|---------|----------|---------|
| `file.txt` | ✅ Pass | `file.txt` | ✅ Pass | ✅ 正常处理 |
| `subdir/file.txt` | ✅ Pass | `subdir/file.txt` | ✅ Pass | ✅ 正常处理 |
| `./file.txt` | ✅ Pass | `file.txt` | ✅ Pass | ✅ 正常处理 |
| `subdir/../file.txt` | ✅ Pass | `file.txt` | ✅ Pass | ✅ 正常处理 (规范化) |
| `a/b/../c/d` | ✅ Pass | `a/c/d` | ✅ Pass | ✅ 正常处理 (规范化) |
| `../file.txt` | ❌ 400 | - | - | ❌ Bay 快速拒绝 (逃逸) |
| `a/../../b.txt` | ❌ 400 | - | - | ❌ Bay 快速拒绝 (逃逸) |
| `/etc/passwd` | ❌ 400 | - | - | ❌ Bay 快速拒绝 (绝对路径) |
| `` (空) | ❌ 400 | - | - | ❌ Bay 快速拒绝 |
| `file\x00.txt` | ❌ 400 | - | - | ❌ Bay 快速拒绝 (null byte) |
| `valid/symlink` | ✅ Pass | `valid/symlink` | ⚠️ 取决于目标 | ⚠️ Ship 处理 |
| `.hidden` | ✅ Pass | `.hidden` | ✅ Pass | ✅ 正常处理 |
| `...file` | ✅ Pass | `...file` | ✅ Pass | ✅ 正常处理 |

## 6. 错误响应示例

### 6.1 Bay 400 响应

```json
{
  "error": {
    "code": "invalid_path",
    "message": "path must be a relative path",
    "details": {
      "field": "path",
      "reason": "absolute_path"
    }
  }
}
```

### 6.2 Ship 403 响应 (不应到达，作为后备)

```json
{
  "detail": "Access denied: path must be within cargo /workspace"
}
```

## 7. 测试计划

### 8.1 单元测试

**文件**: `pkgs/bay/tests/unit/test_path_validator.py`

```python
import pytest
from app.validators.path import validate_relative_path
from app.errors import InvalidPathError

class TestValidateRelativePath:
    """Test path validation with normalization (选项 B)."""

    # --- Valid paths ---

    def test_valid_simple_path(self):
        assert validate_relative_path("file.txt") == "file.txt"

    def test_valid_nested_path(self):
        assert validate_relative_path("a/b/c.txt") == "a/b/c.txt"

    def test_normalizes_dot_prefix(self):
        # ./file.txt -> file.txt (removes .)
        assert validate_relative_path("./file.txt") == "file.txt"

    def test_normalizes_internal_traversal(self):
        # subdir/../file.txt -> file.txt
        assert validate_relative_path("subdir/../file.txt") == "file.txt"

    def test_normalizes_complex_path(self):
        # a/b/../c/d -> a/c/d
        assert validate_relative_path("a/b/../c/d") == "a/c/d"

    def test_normalizes_multiple_dots(self):
        # ./a/./b/./c -> a/b/c
        assert validate_relative_path("./a/./b/./c") == "a/b/c"

    def test_normalizes_to_dot_for_empty_result(self):
        # a/.. -> . (current dir)
        assert validate_relative_path("a/..") == "."

    def test_allows_hidden_files(self):
        assert validate_relative_path(".hidden") == ".hidden"

    def test_allows_triple_dots(self):
        # "..." is not "..", so it's allowed
        assert validate_relative_path("...file") == "...file"

    # --- Invalid paths ---

    def test_rejects_absolute_path(self):
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("/etc/passwd")
        assert exc.value.code == "invalid_path"
        assert exc.value.details["reason"] == "absolute_path"

    def test_rejects_traversal_at_start(self):
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("../file.txt")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_traversal_escaping_workspace(self):
        # a/../../b.txt escapes: a -> . -> error
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("a/../../b.txt")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_deep_traversal(self):
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("../../etc/passwd")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_empty_path(self):
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("")
        assert exc.value.details["reason"] == "empty_path"

    def test_rejects_null_byte(self):
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("file\x00.txt")
        assert exc.value.details["reason"] == "null_byte"
```

### 7.2 E2E 测试

**文件**: `pkgs/bay/tests/integration/test_path_security.py`

```python
class TestPathSecurityE2E:
    async def test_filesystem_rejects_absolute_path(self):
        # GET /v1/sandboxes/{id}/filesystem/files?path=/etc/passwd
        # Expect 400 invalid_path

    async def test_filesystem_rejects_traversal(self):
        # PUT /v1/sandboxes/{id}/filesystem/files
        # Body: {"path": "../secret.txt", "content": "..."}
        # Expect 400 invalid_path

    async def test_shell_rejects_cwd_traversal(self):
        # POST /v1/sandboxes/{id}/shell/exec
        # Body: {"command": "ls", "cwd": "../"}
        # Expect 400 invalid_path

    async def test_upload_rejects_absolute_path(self):
        # POST /v1/sandboxes/{id}/filesystem/upload
        # FormData: file=..., path=/tmp/evil.sh
        # Expect 400 invalid_path
```

## 9. 待决策点

### 9.1 是否允许 `subdir/../file.txt`?

**决策**: ✅ 允许

采用选项 B，Bay 做路径规范化，允许规范化后不逃逸的路径。
- `subdir/../file.txt` → 规范化为 `file.txt` → ✅ 允许
- `../file.txt` → 规范化时逃逸 → ❌ 拒绝

### 9.2 `cwd` 为 `None` 时是否需要校验?

**决策**: 不需要

`None` 表示使用默认 `/workspace`，无需校验。

### 9.3 是否限制路径长度?

**决策**: 可选，建议限制为 4096 字符 (Linux PATH_MAX)

### 9.4 规范化后的路径是否传递给 Ship?

**决策**: 是

Bay 将规范化后的路径传递给 Ship，减少 Ship 的处理负担。
例如：用户请求 `a/b/../c.txt`，Bay 规范化为 `a/c.txt` 后传给 Ship。

---

## ~~8. 待决策点 (已决策)~~

### 8.1 是否允许 `subdir/../file.txt`?

**当前建议**: 不允许

如果需要允许，需在 Bay 侧实现路径规范化，增加复杂度。

### 8.2 `cwd` 为 `None` 时是否需要校验?

**当前建议**: 不需要

`None` 表示使用默认 `/workspace`，无需校验。

### 8.3 是否限制路径长度?

**当前建议**: 可选，建议限制为 4096 字符 (Linux PATH_MAX)

## 9. 实现步骤

1. [x] 新增 `InvalidPathError` 到 `errors.py`
2. [x] 创建 `validators/path.py` 模块
3. [x] 编写单元测试 `test_path_validator.py`
4. [x] 集成到 filesystem API 端点
5. [x] 集成到 shell API 端点 (cwd 字段)
6. [x] 编写 E2E 测试 `test_path_security.py`
7. [x] 更新 TODO.md 标记完成

## 10. 参考

- Ship `resolve_path`: `pkgs/ship/app/workspace.py:26-54`
- Ship `run_command` cwd 处理: `pkgs/ship/app/components/user_manager.py:206-220`
- Bay capabilities API: `pkgs/bay/app/api/v1/capabilities.py`
- Bay CapabilityRouter: `pkgs/bay/app/router/capability/capability.py`
- Bay ShipAdapter: `pkgs/bay/app/adapters/ship.py`
