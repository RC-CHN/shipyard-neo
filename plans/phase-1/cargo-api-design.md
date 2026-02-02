# Workspace 管理 API 设计

> 基于 [bay-api.md 6.3 节](../bay-api.md:308) 的 Workspace 数据面（高级/管理面）API 实现设计。

## 0. 资源模型一致性校验

### 0.1 与 Sandbox API Response 对比

| 字段 | Sandbox Response | Workspace Response | 备注 |
|------|------------------|-------------------|------|
| `id` | ✓ `sandbox-xxx` | ✓ `ws-xxx` | 一致 |
| `status` | ✓ 聚合状态 | ✗ 不需要 | Workspace 无状态机 |
| `created_at` | ✓ ISO 8601 | ✓ ISO 8601 | 一致 |
| `expires_at` | ✓ TTL | ✗ 不需要 | Workspace 无 TTL |
| `owner` | ✗ 不暴露 | ✗ 不暴露 | **保持一致：owner 仅用于过滤，不在响应中暴露** |

### 0.2 字段命名一致性

- `managed_by_sandbox_id`: 与 Sandbox 的 `workspace_id` 形成双向引用
- 时间字段统一使用 `xxx_at` 后缀（`created_at`, `last_accessed_at`）
- ID 字段使用 `xxx_id` 后缀

### 0.3 设计决策点

**Q1: Workspace 是否需要 owner 字段在响应中？**
- **决策**: 不暴露。与 Sandbox API 保持一致，owner 仅用于权限过滤
- **理由**: 减少信息泄露，单租户模式下意义不大

**Q2: 是否需要 `deleted_at` 软删除？**
- **决策**: Phase 1 不需要。Workspace 直接硬删除
- **理由**: Workspace 本身无级联依赖需要保护

## 1. 端点总览

| 方法 | 路径 | 描述 | 权限级别 |
|------|------|------|----------|
| `POST` | `/v1/workspaces` | 创建 external workspace | 标准 |
| `GET` | `/v1/workspaces` | 列出 workspaces | 标准 |
| `GET` | `/v1/workspaces/{id}` | 获取 workspace 详情 | 标准 |
| `DELETE` | `/v1/workspaces/{id}` | 删除 workspace | 标准 |
| `POST` | `/v1/workspaces/{id}/files/read` | 直接读文件 | 高级（Phase 2） |
| `POST` | `/v1/workspaces/{id}/files/write` | 直接写文件 | 高级（Phase 2） |

## 2. 资源模型

### 2.1 Workspace Response

```json
{
  "id": "ws-xyz789",
  "managed": false,
  "managed_by_sandbox_id": null,
  "backend": "docker_volume",
  "size_limit_mb": 1024,
  "created_at": "2026-01-28T06:00:00Z",
  "last_accessed_at": "2026-01-28T06:10:00Z"
}
```

### 2.2 与 Sandbox 的关系

- **managed workspace**: 由 `POST /v1/sandboxes` 隐式创建，生命周期绑定 sandbox
- **external workspace**: 由 `POST /v1/workspaces` 显式创建，独立于任何 sandbox

## 3. 端点详细设计

### 3.1 创建 External Workspace

```
POST /v1/workspaces
```

**Request Body:**
```json
{
  "size_limit_mb": 2048  // optional, defaults to config
}
```

**Response: 201 Created**
```json
{
  "id": "ws-abc123",
  "managed": false,
  "managed_by_sandbox_id": null,
  "backend": "docker_volume",
  "size_limit_mb": 2048,
  "created_at": "2026-01-30T07:00:00Z",
  "last_accessed_at": "2026-01-30T07:00:00Z"
}
```

**语义:**
- 创建独立的 external workspace（`managed=false`）
- 可在后续 `POST /v1/sandboxes` 时通过 `workspace_id` 参数绑定
- 多个 sandbox 可共享同一个 external workspace

### 3.2 列出 Workspaces

```
GET /v1/workspaces?limit=50&cursor=...&managed=false
```

**Query Parameters:**
- `limit`: 默认 50，最大 200
- `cursor`: 分页游标
- `managed`: 可选过滤器（`true`/`false`）

**Response: 200 OK**
```json
{
  "items": [
    {
      "id": "ws-abc123",
      "managed": false,
      "managed_by_sandbox_id": null,
      "backend": "docker_volume",
      "size_limit_mb": 1024,
      "created_at": "...",
      "last_accessed_at": "..."
    }
  ],
  "next_cursor": null
}
```

### 3.3 获取 Workspace 详情

```
GET /v1/workspaces/{id}
```

**Response: 200 OK**
```json
{
  "id": "ws-abc123",
  "managed": false,
  "managed_by_sandbox_id": null,
  "backend": "docker_volume",
  "size_limit_mb": 1024,
  "created_at": "...",
  "last_accessed_at": "..."
}
```

### 3.4 删除 Workspace

```
DELETE /v1/workspaces/{id}
```

**Response: 204 No Content**

**语义:**
- **external workspace**: 直接删除
- **managed workspace**: 
  - 若关联的 sandbox 已删除（`deleted_at` 非空）→ 允许删除
  - 若关联的 sandbox 仍存在 → 返回 `409 Conflict`

**Error Response (409):**
```json
{
  "error": {
    "code": "conflict",
    "message": "Cannot delete managed workspace. Delete the managing sandbox first.",
    "details": {
      "workspace_id": "ws-abc123",
      "managed_by_sandbox_id": "sandbox-xyz"
    }
  }
}
```

## 8. 边界场景分析与推敲

### 场景 1: External Workspace 被多个 Sandbox 共享

**操作序列:**
1. `POST /v1/workspaces` → `ws-001` (external, managed=false)
2. `POST /v1/sandboxes {workspace_id: ws-001}` → `sandbox-A`
3. `POST /v1/sandboxes {workspace_id: ws-001}` → `sandbox-B`
4. `DELETE /v1/sandboxes/sandbox-A` → 成功，`ws-001` 保留
5. `DELETE /v1/workspaces/ws-001` → **？**

**核心问题:** `sandbox-B` 仍在使用 `ws-001`，直接删除会导致悬空引用。

**候选方案分析:**

| 选项 | 行为 | 优点 | 缺点 |
|------|------|------|------|
| A | 返回 409 Conflict | 避免悬空引用，数据一致性强 | 用户必须先删除所有 sandbox |
| B | 允许删除，workspace_id 保持原值 | 灵活，用户可强制清理 | sandbox-B 后续操作失败 |
| C | 允许删除，级联更新 workspace_id=NULL | 保持数据一致 | sandbox 变成无 workspace 状态 |

**✅ 决策: 选项 A - 返回 409 Conflict**

**决策理由:**
1. 保持数据一致性，避免运行时错误
2. 明确的错误信息告知用户需要先清理哪些 sandbox
3. 符合 "fail-fast" 原则，问题在删除时暴露而非运行时

**Error Response:**
```json
{
  "error": {
    "code": "conflict",
    "message": "Workspace is in use by active sandboxes",
    "details": {
      "workspace_id": "ws-001",
      "active_sandbox_ids": ["sandbox-B"]
    }
  }
}
```

**实现要点:**
- 在 `WorkspaceManager.delete()` 中查询所有 `workspace_id == target` 且 `deleted_at IS NULL` 的 sandbox
- 如果列表非空，返回 409 并附上 sandbox ID 列表

### 场景 2: Managed Workspace 的 Sandbox 已软删除

```
操作序列:
1. POST /v1/sandboxes → sandbox-A (自动创建 ws-001, managed=true)
2. DELETE /v1/sandboxes/sandbox-A → sandbox-A.deleted_at 设置
3. （gc 未运行，ws-001 仍存在）
4. DELETE /v1/workspaces/ws-001

预期行为:
- sandbox-A 已软删除，ws-001 应该可以被清理
- 但 WorkspaceManager.delete() 需要检查 sandbox.deleted_at

当前实现问题:
- WorkspaceManager.delete() 检查 managed=True 就拒绝
- 未检查 managed_by_sandbox_id 对应的 sandbox 是否已删除

修复: 增加对 sandbox.deleted_at 的检查
```

### 场景 3: 并发删除 Sandbox 和 Workspace

```
操作序列 (并发):
T1: DELETE /v1/sandboxes/sandbox-A 开始执行
T2: DELETE /v1/workspaces/ws-001 开始执行

潜在问题:
- T1 正在级联删除 managed workspace
- T2 同时尝试删除同一个 workspace
- 可能导致重复删除 volume 或 DB 约束错误

解决方案:
- 使用数据库行锁（SELECT FOR UPDATE）
- 或乐观锁 + 重试
- Phase 1 可接受：让 volume 删除幂等，捕获 NotFound 异常
```

### 场景 4: 创建 Sandbox 时指定不存在的 workspace_id

```
操作序列:
1. POST /v1/sandboxes {workspace_id: ws-not-exist}

预期行为: 400 Bad Request 或 404 Not Found

问题: 现有 SandboxManager.create() 是否校验？

需要确认: 查看 SandboxManager 实现
```

### 场景 5: 创建 Sandbox 时指定别人的 workspace_id

```
操作序列:
1. User A: POST /v1/workspaces → ws-001 (owner=A)
2. User B: POST /v1/sandboxes {workspace_id: ws-001} (owner=B)

预期行为: 403 Forbidden 或 404 Not Found

问题: 需要在 SandboxManager.create() 中校验 workspace.owner == owner
```

### 场景 6: Workspace 列表 - managed 过滤器歧义

```
请求: GET /v1/workspaces?managed=true

问题: 返回所有 managed workspace 有意义吗？
- managed workspace 本质上是 sandbox 的"附属品"
- 用户通常只关心 external workspace

建议:
- 默认只返回 managed=false 的 workspace
- 或者提供 managed 过滤参数但默认不过滤
```

## 9. 修订决策汇总

基于边界场景分析，确定以下决策：

| 场景 | 决策 | 原因 |
|------|------|------|
| 场景 1 | 返回 409 如果仍有 sandbox 引用 | 避免悬空引用 |
| 场景 2 | 允许删除如果 sandbox.deleted_at 非空 | 支持 gc 清理 |
| 场景 3 | Volume 删除幂等 + 捕获异常 | Phase 1 简化方案 |
| 场景 4 | 返回 404 | workspace_id 必须存在 |
| 场景 5 | 返回 404（隐藏权限信息） | 安全考虑 |
| 场景 6 | 默认返回所有，支持 managed 过滤 | 保持灵活性 |

## 10. 需要修改的现有代码

### 10.1 WorkspaceManager.delete() 改进

```python
async def delete(self, workspace_id: str, owner: str, *, force: bool = False) -> None:
    workspace = await self.get(workspace_id, owner)
    
    if workspace.managed and not force:
        # 检查关联的 sandbox 是否已删除
        if workspace.managed_by_sandbox_id:
            sandbox = await self._get_sandbox(workspace.managed_by_sandbox_id)
            if sandbox and sandbox.deleted_at is None:
                raise ConflictError(
                    f"Cannot delete managed workspace. "
                    f"Delete sandbox {workspace.managed_by_sandbox_id} first."
                )
    
    # 检查是否有其他 sandbox 在使用（external workspace 场景）
    if not workspace.managed:
        active_sandboxes = await self._get_sandboxes_using_workspace(workspace_id)
        if active_sandboxes:
            raise ConflictError(
                f"Workspace is in use by {len(active_sandboxes)} sandbox(es). "
                f"Delete them first."
            )
    
    # 继续删除...
```

### 10.2 SandboxManager.create() 增加校验

```python
async def create(self, owner: str, workspace_id: str | None = None, ...) -> Sandbox:
    if workspace_id:
        # 校验 workspace 存在且属于当前用户
        workspace = await self._workspace_mgr.get(workspace_id, owner)
        # get() 已包含 owner 校验，不存在或不属于用户会抛 NotFoundError
```

## 4. 实现计划

### 4.1 Phase 1 核心（本次实现）

1. **新建文件**: `pkgs/bay/app/api/v1/workspaces.py`
   - `POST /v1/workspaces` - 创建 external workspace
   - `GET /v1/workspaces` - 列出 workspaces
   - `GET /v1/workspaces/{id}` - 获取详情
   - `DELETE /v1/workspaces/{id}` - 删除

2. **更新依赖注入**: `pkgs/bay/app/api/dependencies.py`
   - 添加 `get_workspace_manager` 函数
   - 添加 `WorkspaceManagerDep` 类型别名

3. **注册路由**: `pkgs/bay/app/api/v1/__init__.py`
   - 添加 workspaces router

### 4.2 Phase 2（后续扩展）

- `POST /v1/workspaces/{id}/files/read` - 直接读文件
- `POST /v1/workspaces/{id}/files/write` - 直接写文件
- 更严格的权限控制（需要 admin scope）
- 审计日志

## 5. WorkspaceManager 改动

现有的 [`WorkspaceManager`](../../pkgs/bay/app/managers/workspace/workspace.py:23) 已实现：
- `create()`: ✓ 已支持 `managed` 参数
- `get()`: ✓ 已有 owner 校验
- `list()`: ✓ 已支持分页
- `delete()`: ✓ 已有 managed 校验

需要添加：
- `list()` 增加 `managed` 过滤参数

## 6. 测试策略

### 6.1 单元测试

- WorkspaceManager CRUD 操作
- managed workspace 删除保护逻辑

### 6.2 集成测试

- 创建 external workspace → 绑定 sandbox → 删除 sandbox → workspace 保留
- 创建 sandbox（managed workspace）→ 尝试直接删除 workspace → 409
- 列出 workspaces 分页与过滤

## 7. 代码结构

```
pkgs/bay/app/
├── api/
│   └── v1/
│       ├── __init__.py          # 添加 workspaces router
│       ├── workspaces.py        # 新建 - Workspace API
│       └── sandboxes.py         # 现有
├── managers/
│   └── workspace/
│       └── workspace.py         # 已有 - 添加 managed 过滤
└── models/
    └── workspace.py             # 已有
```
