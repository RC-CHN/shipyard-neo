# Bay Capability 适配器设计

> 日期：2026-01-29
>
> 状态：**Approved**
>
> 解决的问题：
> 1. meta 里的能力名映射不一致（Ship 返回 `python`，config profile 用 `ipython`）
> 2. 缺少 upload/download 支持
> 3. CapabilityRouter 和 RuntimeClient 的关系不清晰
> 4. 希望支持不同镜像实现（解耦）

## 1. 设计决策

| 决策点 | 决定 |
|:--|:--|
| **架构方案** | 方案 A：合并 - 删除 Client，只用 Adapter |
| **upload/download API 路径** | `files/upload` 和 `files/download` |
| **能力校验时机** | 首次 ensure_running 时校验并缓存 |
| **clients/runtime/ 目录** | 删除 |
| **能力名称变更** | config + API 全改（ipython → python） |

## 2. 目标架构

### 2.1 目录结构

```
pkgs/bay/app/
├── adapters/                    # 新增
│   ├── __init__.py
│   ├── base.py                  # BaseAdapter 抽象类
│   └── ship.py                  # ShipAdapter 实现
│
├── clients/runtime/             # 删除整个目录
│
├── router/capability/
│   └── capability.py            # 修改：使用 Adapter
│
├── api/v1/
│   └── capabilities.py          # 修改：ipython→python，新增 upload/download
│
└── config.py                    # 修改：capabilities 默认值
```

### 2.2 类图

```
┌─────────────────────────────────────────────────────────────────────┐
│                          CapabilityRouter                            │
│  - 负责 session 管理（ensure_running）                                │
│  - 获取/创建 Adapter                                                 │
│  - 调度能力请求                                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ uses
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           BaseAdapter                                │
│  + get_meta() -> RuntimeMeta                                        │
│  + health() -> bool                                                 │
│  + supported_capabilities() -> list[str]                            │
│  + exec_python(code, timeout) -> ExecutionResult                    │
│  + exec_shell(command, timeout, cwd) -> ExecutionResult             │
│  + read_file(path) -> str                                           │
│  + write_file(path, content) -> None                                │
│  + list_files(path) -> list[dict]                                   │
│  + delete_file(path) -> None                                        │
│  + upload_file(path, content: bytes) -> None                        │
│  + download_file(path) -> bytes                                     │
└──────────────────────────────┬──────────────────────────────────────┘
                               △
                               │ extends
                               │
┌─────────────────────────────────────────────────────────────────────┐
│                           ShipAdapter                                │
│  - base_url: str                                                    │
│  - _meta_cache: RuntimeMeta | None                                  │
│  + 实现所有能力方法                                                  │
│  + HTTP 调用 Ship 容器                                               │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. 接口设计

### 3.1 BaseAdapter

```python
# pkgs/bay/app/adapters/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeMeta:
    """Runtime 元数据。"""
    name: str
    version: str
    api_version: str
    mount_path: str
    capabilities: dict[str, Any]


@dataclass
class ExecutionResult:
    """执行结果。"""
    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    data: dict[str, Any] | None = None


class BaseAdapter(ABC):
    """Runtime 适配器基类。
    
    每个 runtime 镜像实现一个适配器。
    适配器负责：
    1. HTTP 通信
    2. 能力方法实现
    3. 错误映射
    4. meta 缓存
    """

    @abstractmethod
    async def get_meta(self) -> RuntimeMeta:
        """获取 runtime 元数据（应实现缓存）。"""
        ...

    @abstractmethod
    async def health(self) -> bool:
        """健康检查。"""
        ...

    @abstractmethod
    def supported_capabilities(self) -> list[str]:
        """此适配器代码层面支持的能力列表。"""
        ...

    # -- Python 能力 --
    async def exec_python(
        self,
        code: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        raise NotImplementedError("python capability not supported")

    # -- Shell 能力 --
    async def exec_shell(
        self,
        command: str,
        *,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError("shell capability not supported")

    # -- Filesystem 能力 --
    async def read_file(self, path: str) -> str:
        raise NotImplementedError("filesystem capability not supported")

    async def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError("filesystem capability not supported")

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        raise NotImplementedError("filesystem capability not supported")

    async def delete_file(self, path: str) -> None:
        raise NotImplementedError("filesystem capability not supported")

    # -- Upload/Download 能力 --
    async def upload_file(self, path: str, content: bytes) -> None:
        raise NotImplementedError("upload capability not supported")

    async def download_file(self, path: str) -> bytes:
        raise NotImplementedError("download capability not supported")
```

### 3.2 ShipAdapter

```python
# pkgs/bay/app/adapters/ship.py
import httpx
import structlog
from typing import Any

from app.adapters.base import BaseAdapter, RuntimeMeta, ExecutionResult
from app.errors import ShipError, TimeoutError

logger = structlog.get_logger()


class ShipAdapter(BaseAdapter):
    """Ship runtime 适配器。
    
    支持的能力：python, shell, filesystem, upload, download, terminal
    """

    SUPPORTED_CAPABILITIES = [
        "python",
        "shell", 
        "filesystem",
        "upload",
        "download",
        "terminal",
    ]

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._meta_cache: RuntimeMeta | None = None
        self._log = logger.bind(adapter="ship", base_url=base_url)

    def supported_capabilities(self) -> list[str]:
        return self.SUPPORTED_CAPABILITIES

    async def get_meta(self) -> RuntimeMeta:
        """获取 runtime 元数据（带缓存）。"""
        if self._meta_cache is not None:
            return self._meta_cache

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._base_url}/meta",
                    timeout=5.0
                )
                response.raise_for_status()
                data = response.json()
        except httpx.RequestError as e:
            raise ShipError(f"Failed to get meta: {e}")

        runtime = data.get("runtime", {})
        workspace = data.get("workspace", {})
        capabilities = data.get("capabilities", {})

        self._meta_cache = RuntimeMeta(
            name=runtime.get("name", "ship"),
            version=runtime.get("version", "unknown"),
            api_version=runtime.get("api_version", "v1"),
            mount_path=workspace.get("mount_path", "/workspace"),
            capabilities=capabilities,
        )
        
        self._log.info(
            "adapter.meta_cached",
            name=self._meta_cache.name,
            version=self._meta_cache.version,
            capabilities=list(capabilities.keys()),
        )
        
        return self._meta_cache

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._base_url}/health",
                    timeout=5.0
                )
                return response.status_code == 200
        except Exception:
            return False

    # -- Python --
    async def exec_python(
        self,
        code: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/ipython/exec",
                    json={"code": code, "timeout": timeout, "silent": False},
                    timeout=timeout + 5,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException:
            raise TimeoutError(f"Python execution timed out after {timeout}s")
        except httpx.RequestError as e:
            raise ShipError(f"Python execution failed: {e}")

        output_obj = result.get("output") or {}
        output_text = output_obj.get("text", "") if isinstance(output_obj, dict) else ""

        return ExecutionResult(
            success=bool(result.get("success", False)),
            output=output_text,
            error=result.get("error"),
            data={
                "execution_count": result.get("execution_count"),
                "output": output_obj,
            },
        )

    # -- Shell --
    async def exec_shell(
        self,
        command: str,
        *,
        timeout: int = 30,
        cwd: str | None = None,
    ) -> ExecutionResult:
        payload: dict[str, Any] = {"command": command, "timeout": timeout}
        if cwd:
            payload["cwd"] = cwd

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/shell/exec",
                    json=payload,
                    timeout=timeout + 5,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException:
            raise TimeoutError(f"Shell execution timed out after {timeout}s")
        except httpx.RequestError as e:
            raise ShipError(f"Shell execution failed: {e}")

        return ExecutionResult(
            success=result.get("exit_code", -1) == 0,
            output=result.get("output", ""),
            error=result.get("error"),
            exit_code=result.get("exit_code"),
            data={"raw": result},
        )

    # -- Filesystem --
    async def read_file(self, path: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/fs/read_file",
                    json={"path": path},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                return response.json().get("content", "")
        except httpx.RequestError as e:
            raise ShipError(f"Read file failed: {e}")

    async def write_file(self, path: str, content: str) -> None:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/fs/write_file",
                    json={"path": path, "content": content, "mode": "w"},
                    timeout=self._timeout,
                )
                response.raise_for_status()
        except httpx.RequestError as e:
            raise ShipError(f"Write file failed: {e}")

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/fs/list_dir",
                    json={"path": path, "show_hidden": False},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                return response.json().get("files", [])
        except httpx.RequestError as e:
            raise ShipError(f"List files failed: {e}")

    async def delete_file(self, path: str) -> None:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self._base_url}/fs/delete_file",
                    json={"path": path},
                    timeout=self._timeout,
                )
                response.raise_for_status()
        except httpx.RequestError as e:
            raise ShipError(f"Delete file failed: {e}")

    # -- Upload/Download --
    async def upload_file(self, path: str, content: bytes) -> None:
        try:
            async with httpx.AsyncClient() as client:
                files = {"file": ("file", content, "application/octet-stream")}
                data = {"file_path": path}
                response = await client.post(
                    f"{self._base_url}/upload",
                    files=files,
                    data=data,
                    timeout=self._timeout,
                )
                response.raise_for_status()
        except httpx.RequestError as e:
            raise ShipError(f"Upload file failed: {e}")

    async def download_file(self, path: str) -> bytes:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._base_url}/download",
                    params={"file_path": path},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                return response.content
        except httpx.RequestError as e:
            raise ShipError(f"Download file failed: {e}")
```

## 4. API 变更

### 4.1 能力名称映射关系

**三层命名对应**：

```
┌────────────────┬─────────────────┬──────────────────────────────────────┐
│  层级          │  名称           │  说明                                │
├────────────────┼─────────────────┼──────────────────────────────────────┤
│  Bay API       │  /python/exec   │  对外暴露的 API 路径（不变）          │
│  Bay Config    │  python         │  profile.capabilities 配置（改）     │
│  Ship meta     │  python         │  /meta 返回的 capabilities key       │
│  Ship 内部端点 │  /ipython/exec  │  Ship 实现细节，Adapter 内部知道     │
└────────────────┴─────────────────┴──────────────────────────────────────┘
```

**本次变更范围**：

| 位置 | 旧值 | 新值 | 是否修改 |
|:--|:--|:--|:--|
| Bay API path | `/python/exec` | - | ❌ 不变 |
| Bay config `capabilities` | `["ipython", ...]` | `["python", ...]` | ✅ 修改 |
| Ship `/meta` | `capabilities.python` | - | ❌ 不变（已经是 python） |
| Ship 内部端点 | `/ipython/exec` | - | ❌ 不变 |

**ShipAdapter 的职责**：
- ShipAdapter 知道 Bay 的 `python` 能力对应 Ship 的 `/ipython/exec` 端点
- 这个映射封装在 [`ShipAdapter.exec_python()`](plans/phase-1/capability-adapter-design.md:266) 内部
- 对 CapabilityRouter 和上层 API 完全透明

### 4.2 新增 upload/download API

```python
# pkgs/bay/app/api/v1/capabilities.py (新增)

from fastapi import UploadFile, File, Form, Query
from fastapi.responses import Response
from pathlib import Path


@router.post("/{sandbox_id}/files/upload", status_code=200)
async def upload_file(
    sandbox_id: str,
    file: UploadFile = File(...),
    path: str = Form(..., description="Target path relative to /workspace"),
    sandbox_mgr: SandboxManagerDep = None,
    owner: OwnerDep = None,
) -> dict[str, Any]:
    """Upload binary file to sandbox.
    
    Args:
        sandbox_id: Sandbox ID
        file: File to upload (multipart/form-data)
        path: Target path in workspace
    
    Returns:
        Upload result with path and size
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)
    
    content = await file.read()
    await capability_router.upload_file(sandbox=sandbox, path=path, content=content)
    
    return {"status": "ok", "path": path, "size": len(content)}


@router.get("/{sandbox_id}/files/download")
async def download_file(
    sandbox_id: str,
    path: str = Query(..., description="File path relative to /workspace"),
    sandbox_mgr: SandboxManagerDep = None,
    owner: OwnerDep = None,
) -> Response:
    """Download file from sandbox.
    
    Args:
        sandbox_id: Sandbox ID
        path: File path in workspace
    
    Returns:
        File content as binary stream
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)
    
    content = await capability_router.download_file(sandbox=sandbox, path=path)
    filename = Path(path).name
    
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

### 4.3 完整 API 列表

| HTTP Method | Path | 说明 |
|:--|:--|:--|
| `POST` | `/{sandbox_id}/python/exec` | 执行 Python 代码 |
| `POST` | `/{sandbox_id}/shell/exec` | 执行 Shell 命令 |
| `POST` | `/{sandbox_id}/files/read` | 读取文本文件 |
| `POST` | `/{sandbox_id}/files/write` | 写入文本文件 |
| `POST` | `/{sandbox_id}/files/list` | 列出目录 |
| `POST` | `/{sandbox_id}/files/delete` | 删除文件/目录 |
| `POST` | `/{sandbox_id}/files/upload` | **新增** - 上传二进制文件 |
| `GET` | `/{sandbox_id}/files/download` | **新增** - 下载文件 |

## 5. 能力校验

### 5.1 校验时机

在 `ensure_running` 完成后，首次使用 Adapter 时调用 `get_meta()` 并校验。

### 5.2 校验逻辑

```python
# pkgs/bay/app/router/capability/capability.py

async def _validate_capabilities(
    self,
    adapter: BaseAdapter,
    required: list[str],
) -> None:
    """校验 runtime 是否支持所需能力。
    
    Raises:
        CapabilityNotSupportedError: 如果缺少必需能力
    """
    meta = await adapter.get_meta()
    
    for cap in required:
        if cap not in meta.capabilities:
            raise CapabilityNotSupportedError(
                message=f"Runtime does not support capability: {cap}",
                capability=cap,
                available=list(meta.capabilities.keys()),
            )
```

### 5.3 缓存策略

- `ShipAdapter.get_meta()` 内部缓存 `RuntimeMeta`
- 每个 Adapter 实例只调用一次 `/meta`
- Adapter 实例按 endpoint 缓存在 `CapabilityRouter._adapters`

## 6. 配置变更

### 6.1 config.py

```python
# pkgs/bay/app/config.py line 94
class ProfileConfig(BaseModel):
    capabilities: list[str] = Field(
        default_factory=lambda: ["filesystem", "shell", "python"]  # 改自 ipython
    )
```

### 6.2 config.yaml / config.yaml.example

```yaml
profiles:
  - id: python-default
    image: ship:latest
    capabilities:
      - python       # 改自 ipython
      - shell
      - filesystem
      - upload
      - download
```

## 7. 需要修改的文件清单

| 文件 | 操作 | 说明 |
|:--|:--|:--|
| `adapters/__init__.py` | 新建 | 导出 BaseAdapter, ShipAdapter |
| `adapters/base.py` | 新建 | 抽象基类 |
| `adapters/ship.py` | 新建 | Ship 适配器实现 |
| `clients/runtime/` | **删除目录** | 合并到 adapters |
| `router/capability/capability.py` | 修改 | 使用 Adapter，添加 upload/download 方法 |
| `api/v1/capabilities.py` | 修改 | 添加 upload/download 端点 |
| `config.py` | 修改 | capabilities 默认值 |
| `config.yaml` | 修改 | capabilities 配置 |
| `config.yaml.example` | 修改 | 同上 |
| `tests/unit/test_ship_client.py` | 重命名/修改 | → test_ship_adapter.py |
| `tests/integration/test_e2e_api.py` | 修改 | 添加 upload/download 测试 |
| `tests/fakes.py` | 修改 | FakeAdapter 替代 FakeDriver（如需） |

## 8. 测试计划

### 8.1 单元测试

| 测试文件 | 测试内容 |
|:--|:--|
| `test_ship_adapter.py` | ShipAdapter 各方法的请求/响应解析 |
| `test_capability_router.py` | CapabilityRouter 的能力校验和调度 |

### 8.2 E2E 测试（新增）

```python
# tests/integration/test_e2e_api.py

class TestE2E05UploadDownload:
    """E2E-05: Upload and download files."""

    async def test_upload_and_download(self):
        """Upload a file and download it back."""
        async with httpx.AsyncClient(...) as client:
            # Create sandbox
            sandbox = await create_sandbox(client)
            
            # Upload binary file
            content = b"\x00\x01\x02\x03 binary content"
            upload_response = await client.post(
                f"/v1/sandboxes/{sandbox['id']}/files/upload",
                files={"file": ("test.bin", content)},
                data={"path": "test.bin"},
            )
            assert upload_response.status_code == 200
            
            # Download and verify
            download_response = await client.get(
                f"/v1/sandboxes/{sandbox['id']}/files/download",
                params={"path": "test.bin"},
            )
            assert download_response.status_code == 200
            assert download_response.content == content
```

## 9. 迁移步骤（执行顺序）

1. [ ] 创建 `adapters/` 目录和文件
2. [ ] 修改 `CapabilityRouter` 使用 Adapter
3. [ ] 删除 `clients/runtime/` 目录
4. [ ] 添加 upload/download API
5. [ ] 更新 config（ipython → python）
6. [ ] 更新/重命名测试文件
7. [ ] 运行所有测试验证

---

## 附录：决策记录

| 日期 | 决策 | 理由 |
|:--|:--|:--|
| 2026-01-29 | 采用方案 A（合并） | 简单直接，当前阶段不需要过度设计 |
| 2026-01-29 | upload/download 放 files/ 下 | 与其他 files API 保持一致 |
| 2026-01-29 | 首次校验并缓存 | 平衡性能和正确性 |
| 2026-01-29 | 删除 clients/runtime/ | 避免两套并存造成混乱 |
| 2026-01-29 | config + API 全改 | 保持一致性，避免 ipython/python 混用 |
