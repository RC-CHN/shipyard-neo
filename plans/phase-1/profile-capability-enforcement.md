# Profile 级别能力检查设计

> **状态**: ✅ 已实现
> **创建时间**: 2026-01-30
> **更新时间**: 2026-01-30
> **关联**: [capability-adapter-design.md](./capability-adapter-design.md)

## 1. 问题背景

### 1.1 当前架构

```
┌──────────────────────────────────────────────────────────────────┐
│                          Bay API Layer                            │
│   POST /v1/sandboxes/{id}/python/exec                            │
│   POST /v1/sandboxes/{id}/shell/exec                             │
│   GET  /v1/sandboxes/{id}/filesystem/files                       │
│   ...                                                             │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                      CapabilityRouter                             │
│   1. ensure_session: sandbox → running session                    │
│   2. _get_adapter: session.runtime_type → adapter                 │
│   3. _require_capability: adapter.get_meta().capabilities 检查    │  ← 运行时级别检查
│   4. 调用 adapter.exec_python / exec_shell / ...                  │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                       ShipAdapter                                 │
│   HTTP 调用 Ship 容器的 /python/exec, /shell/exec 等              │
└──────────────────────────────────────────────────────────────────┘
```

### 1.2 当前能力检查位置

| 检查点 | 位置 | 检查内容 | 问题 |
|--------|------|----------|------|
| Runtime 级别 | `CapabilityRouter._require_capability()` | 调用 Ship `/meta` 接口，检查运行时实际支持的能力 | 需要先启动容器才能检查 |
| Profile 声明 | `config.yaml` profiles[].capabilities | 声明 profile 支持哪些能力 | **未被校验，仅用于 API 返回** |

### 1.3 问题场景

**场景 1: Profile 子集能力**
```yaml
profiles:
  - id: python-default
    capabilities: [filesystem, shell, python]
    
  - id: python-readonly  # 只读 profile
    capabilities: [python]  # 不允许 shell 和 filesystem 写入
```

当前：用户创建 `python-readonly` sandbox 后调用 `/shell/exec`，请求会：
1. 启动容器（浪费资源）
2. 调用 Ship `/meta`
3. Ship 返回支持 shell
4. 成功执行 ← **与 profile 声明不符**

**场景 2: 新增能力类型**
```yaml
profiles:
  - id: browser-sandbox
    image: browser:latest
    runtime_type: browser
    capabilities: [browser, filesystem]  # 新能力类型
```

当前：
- 需要修改 Bay 代码添加 `/browser/...` 路由
- 需要添加 BrowserAdapter
- Profile 声明的 capabilities 可以自动限制路由

## 2. 设计目标

1. **Profile 级别前置检查**: 在启动容器前就拒绝不支持的能力请求
2. **声明即约束**: `config.yaml` 中 profile 声明的 capabilities 成为硬约束
3. **向后兼容**: 现有 profile 和 API 不受影响
4. **易于扩展**: 新增能力时改动最小化

## 3. 设计方案

### 3.1 能力层次定义

```
┌─────────────────────────────────────────────────────────────────┐
│ Level 1: Profile 声明能力 (config.yaml)                         │
│   - 定义该 profile 允许使用的能力                                │
│   - Bay 层前置检查，不启动容器就拒绝                             │
├─────────────────────────────────────────────────────────────────┤
│ Level 2: Runtime 实际能力 (/meta)                                │
│   - 定义该运行时镜像实际实现的能力                               │
│   - 容器启动后检查，作为二次保障                                 │
└─────────────────────────────────────────────────────────────────┘

规则: Profile 声明能力 ⊆ Runtime 实际能力
```

### 3.2 实现方案: 依赖注入

在 `app/api/v1/capabilities.py` 每个 endpoint 添加能力检查依赖：

```python
# app/api/dependencies.py

from app.config import get_settings
from app.errors import CapabilityNotSupportedError


def require_capability(capability: str):
    """Factory for capability check dependency."""
    
    async def dependency(
        sandbox_id: str,
        sandbox_mgr: SandboxManagerDep,
        owner: AuthDep,
    ) -> Sandbox:
        sandbox = await sandbox_mgr.get(sandbox_id, owner)
        settings = get_settings()
        profile = settings.get_profile(sandbox.profile_id)
        
        if profile is None:
            raise CapabilityNotSupportedError(
                message=f"Profile not found: {sandbox.profile_id}",
                capability=capability,
            )
        
        if capability not in profile.capabilities:
            raise CapabilityNotSupportedError(
                message=f"Profile '{sandbox.profile_id}' does not support capability: {capability}",
                capability=capability,
                available=profile.capabilities,
            )
        
        return sandbox
    
    return dependency


# 使用类型别名简化
PythonCapabilityDep = Annotated[Sandbox, Depends(require_capability("python"))]
ShellCapabilityDep = Annotated[Sandbox, Depends(require_capability("shell"))]
FilesystemCapabilityDep = Annotated[Sandbox, Depends(require_capability("filesystem"))]
```

```python
# app/api/v1/capabilities.py

@router.post("/{sandbox_id}/python/exec", response_model=PythonExecResponse)
async def exec_python(
    request: PythonExecRequest,
    sandbox: PythonCapabilityDep,  # 自动检查 python 能力
    sandbox_mgr: SandboxManagerDep,
) -> PythonExecResponse:
    ...
```

### 3.3 能力映射表

| API Endpoint | 需要的能力 |
|--------------|-----------|
| `POST /{id}/python/exec` | python |
| `POST /{id}/shell/exec` | shell |
| `GET /{id}/filesystem/files` | filesystem |
| `PUT /{id}/filesystem/files` | filesystem |
| `DELETE /{id}/filesystem/files` | filesystem |
| `GET /{id}/filesystem/directories` | filesystem |
| `POST /{id}/filesystem/upload` | filesystem |
| `GET /{id}/filesystem/download` | filesystem |

### 3.4 错误响应格式

```json
{
  "error": {
    "code": "capability_not_supported",
    "message": "Profile 'python-readonly' does not support capability: shell",
    "details": {
      "capability": "shell",
      "available": ["python", "filesystem"]
    }
  }
}
```

HTTP Status: `400 Bad Request`

## 4. 待讨论/敲定的问题

### 4.1 细粒度能力 vs 粗粒度能力

**当前**: `filesystem` 是单一能力，包含 read/write/delete/upload/download

**选项 A: 保持粗粒度 (推荐 Phase 1)**
```yaml
capabilities:
  - filesystem  # 全部文件操作
  - shell
  - python
```

**选项 B: 细粒度能力**
```yaml
capabilities:
  - filesystem:read
  - filesystem:list
  - python
  # 不包含 filesystem:write, shell
```

**建议**: Phase 1 保持粗粒度，未来按需细化

### 4.2 Profile 能力与 Runtime 能力不一致处理

**场景**: Profile 声明 `[python]`，但 Ship 镜像实际支持 `[python, shell, filesystem]`

**选项 A: Profile 优先 (推荐)**
- Profile 声明是策略约束
- 即使运行时支持，也不允许使用
- 更安全，易于理解

**选项 B: 交集**
- 取 Profile ∩ Runtime
- 复杂度较高

**建议**: 采用选项 A，Profile 声明优先

### 4.3 是否需要 CapabilityRouter 层的二次检查

当前 `CapabilityRouter._require_capability()` 检查运行时能力

**选项 A: 保留双重检查**
- 依赖注入检查 Profile 能力（Bay 层）
- _require_capability 检查 Runtime 能力（容器层）
- 双保险

**选项 B: 仅保留 Profile 检查**
- 移除 _require_capability
- 信任 Profile 配置正确

**建议**: Phase 1 采用选项 A，双重检查更安全

### 4.4 新能力类型扩展流程

添加新能力（如 `browser`）需要：

1. 在 `config.yaml` 添加新 profile:
   ```yaml
   - id: browser-default
     capabilities: [browser, filesystem]
   ```

2. 添加新 Adapter:
   ```python
   # app/adapters/browser.py
   class BrowserAdapter(BaseAdapter): ...
   ```

3. 添加新 API endpoints:
   ```python
   # app/api/v1/capabilities.py
   @router.post("/{sandbox_id}/browser/navigate")
   async def browser_navigate(sandbox: BrowserCapabilityDep, ...): ...
   ```

4. 添加能力依赖:
   ```python
   BrowserCapabilityDep = Annotated[Sandbox, Depends(require_capability("browser"))]
   ```

**这是可接受的，因为新能力本身就需要新代码**

## 5. 实现状态 ✅

### 5.1 任务清单（全部完成）

- [x] 添加 `require_capability()` 依赖工厂函数
- [x] 为每个 capability endpoint 添加依赖注入
- [x] 添加单元测试：profile 不支持能力时返回 400
- [x] 添加集成测试：创建受限 profile sandbox，验证能力拦截

### 5.2 已变更文件

| 文件 | 变更 | 状态 |
|------|------|------|
| `app/api/dependencies.py` | 添加 `require_capability()` 工厂 | ✅ |
| `app/api/v1/capabilities.py` | 每个 endpoint 使用能力依赖 | ✅ |
| `tests/unit/test_capability_check.py` | 6 个单元测试 | ✅ |
| `tests/integration/test_capability_enforcement.py` | 11 个集成测试 | ✅ |

## 5.3 完整校验架构

实现后的完整能力校验流程：

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         API Request                                           │
│   POST /v1/sandboxes/{sandbox_id}/shell/exec                                  │
│   Headers: Authorization: Bearer xxx                                          │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                     Level 0: Authentication (AuthDep)                         │
│   dependencies.authenticate(request) → owner                                  │
│   • 验证 API Key                                                              │
│   • 失败: 401 Unauthorized                                                    │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                 Level 1: Profile 能力检查 (ShellCapabilityDep)                │
│   require_capability("shell")                                                 │
│   1. sandbox_mgr.get(sandbox_id, owner) → 获取 sandbox                        │
│   2. settings.get_profile(sandbox.profile_id) → 获取 profile 配置             │
│   3. if "shell" not in profile.capabilities:                                  │
│        raise CapabilityNotSupportedError(400)  ← 前置拦截，不启动容器          │
│   4. return sandbox                                                           │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         API Endpoint Handler                                  │
│   exec_shell(request, sandbox, sandbox_mgr)                                   │
│   • sandbox 已通过能力检查                                                     │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         CapabilityRouter                                      │
│   capability_router.exec_shell(sandbox, command, timeout, cwd)                │
│   1. ensure_session(sandbox) → 确保容器运行中                                  │
│   2. _get_adapter(session) → 根据 runtime_type 获取 Adapter                   │
│   3. _require_capability(adapter, "shell") ← Level 2 二次校验                  │
│      • 调用 adapter.get_meta() 获取运行时实际能力                              │
│      • 如果运行时不支持 shell → 502 Bad Gateway                                │
│   4. adapter.exec_shell(...) → 调用 Ship HTTP API                             │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                Level 2: Runtime 能力检查 (ShipAdapter)                        │
│   GET http://ship-container:8123/meta                                         │
│   Response: { "capabilities": ["shell", "python", "filesystem"] }             │
│   • 验证运行时实际支持请求的能力                                                │
│   • 作为 Profile 检查的二次保障（防御性编程）                                   │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Ship Container                                        │
│   POST http://ship-container:8123/shell/exec                                  │
│   { "command": "echo hello", "timeout": 30 }                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.4 检查层级总结

| 层级 | 位置 | 检查内容 | 错误码 | 说明 |
|:-----|:-----|:---------|:-------|:-----|
| Level 0 | `AuthDep` | API Key 认证 | 401 | 请求身份验证 |
| Level 1 | `require_capability()` | Profile 声明的能力 | 400 | **前置检查，不启动容器** |
| Level 2 | `CapabilityRouter._require_capability()` | Runtime /meta 返回的能力 | 502 | 二次保障（防御性） |

### 5.5 关键优势

1. **资源节省**: 不符合 Profile 的请求在启动容器前就被拒绝
2. **策略与实现分离**: Profile 定义策略，Runtime 定义实现
3. **双重保障**: 即使配置错误，Runtime 检查仍然生效
4. **易于扩展**: 新能力只需添加一行 `XxxCapabilityDep`

## 6. 未来扩展

### 6.1 动态能力发现（Phase 2+）

```
GET /v1/sandboxes/{id}/capabilities

Response:
{
  "capabilities": {
    "python": {
      "enabled": true,
      "operations": ["exec"]
    },
    "shell": {
      "enabled": false,
      "reason": "Profile does not allow shell access"
    },
    "filesystem": {
      "enabled": true,
      "operations": ["read", "write", "list", "delete", "upload", "download"]
    }
  }
}
```

### 6.2 细粒度 RBAC（Phase 3+）

```yaml
profiles:
  - id: analyst
    capabilities:
      python:
        allowed: true
      filesystem:
        allowed: true
        operations: [read, list]  # 只读
      shell:
        allowed: false
```

---

## 附录: 相关代码位置

- Profile 配置: [`app/config.py`](../../pkgs/bay/app/config.py) - `ProfileConfig`
- 能力路由: [`app/router/capability/capability.py`](../../pkgs/bay/app/router/capability/capability.py)
- API endpoints: [`app/api/v1/capabilities.py`](../../pkgs/bay/app/api/v1/capabilities.py)
- 错误类型: [`app/errors.py`](../../pkgs/bay/app/errors.py) - `CapabilityNotSupportedError`
