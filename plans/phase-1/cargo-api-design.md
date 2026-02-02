# Workspace 管理 API 设计

> 基于 [bay-api.md 6.3 节](../bay-api.md:308) 的 Workspace 数据面（高级/管理面）API 实现设计。

## 0. 实现状态与差异说明（截至 2026-02-03）

本文件最初按 Phase 1 规划了对外的 `/v1/workspaces` 管理面 API；但当前代码库的实际落地情况是：

- **已落地：Workspace 数据模型与内部管理能力**
  - Workspace 模型：[`Workspace`](../../pkgs/bay/app/models/workspace.py:19)
  - Workspace 内部管理器：[`WorkspaceManager`](../../pkgs/bay/app/managers/workspace/workspace.py:23)
    - `create/get/list/delete/touch/delete_internal_by_id` 已实现
- **已落地：Sandbox 对 Workspace 的集成与级联行为**
  - 创建 sandbox 时：可复用 external workspace（通过 [`WorkspaceManager.get()`](../../pkgs/bay/app/managers/workspace/workspace.py:90) 做存在性 + owner 校验），否则创建 managed workspace：[`SandboxManager.create()`](../../pkgs/bay/app/managers/sandbox/sandbox.py:56)
  - 删除 sandbox 时：先软删 sandbox（`deleted_at`），再对 managed workspace 走内部级联删除（`force=True`）：[`SandboxManager.delete()`](../../pkgs/bay/app/managers/sandbox/sandbox.py:383)
- **已落地：GC 清理 orphan managed workspaces**
  - 触发条件与动作：[`OrphanWorkspaceGC`](../../pkgs/bay/app/services/gc/tasks/orphan_workspace.py:23)
  - 通过 [`WorkspaceManager.delete_internal_by_id()`](../../pkgs/bay/app/managers/workspace/workspace.py:212) 执行“无需 owner、幂等”的内部清理
- **未落地：对外的 `/v1/workspaces` API 路由**
  - 规划新增的 [`pkgs/bay/app/api/v1/workspaces.py`](../../pkgs/bay/app/api/v1/workspaces.py:1) **当前不存在**
  - v1 路由注册点当前仅包含 sandboxes/capabilities/profiles/admin：[`pkgs/bay/app/api/v1/__init__.py`](../../pkgs/bay/app/api/v1/__init__.py:1)
  - 因此：**目前对外入口仍是 sandbox 面**（例如创建：[`create_sandbox()`](../../pkgs/bay/app/api/v1/sandboxes.py:79)）

### 0.1 与原设计的“潜在冲突点”总结

- 本文原设计中提到“managed workspace 若其 managing sandbox 已软删则允许通过 Workspace API 删除”，但当前实现是：
  - 对外删除：[`WorkspaceManager.delete()`](../../pkgs/bay/app/managers/workspace/workspace.py:158) **managed 一律 409**（除非内部 `force=True`）
  - 后续清理：交给 [`OrphanWorkspaceGC.run()`](../../pkgs/bay/app/services/gc/tasks/orphan_workspace.py:50)
  - 结论：**语义不冲突**，但“允许手工删除 managed workspace”的路径在现阶段不存在（因为 API 未暴露）。
- 本文原设计中对 external workspace 的删除建议“若仍被活跃 sandbox 引用则 409”，但当前 `WorkspaceManager.delete()` **尚未实现引用检查**。
  - 结论：**现阶段不冲突**（因为 `/v1/workspaces` 未对外提供），但若未来补齐 API，需要把该检查作为“必须项”。

---

## 1. 资源模型一致性校验

### 1.1 与 Sandbox API Response 对比

| 字段 | Sandbox Response | Workspace Response | 备注 |
|------|------------------|-------------------|------|
| `id` | ✓ `sandbox-xxx` | ✓ `ws-xxx` | 一致 |
| `status` | ✓ 聚合状态 | ✗ 不需要 | Workspace 无状态机 |
| `created_at` | ✓ ISO 8601 | ✓ ISO 8601 | 一致 |
| `expires_at` | ✓ TTL | ✗ 不需要 | Workspace 无 TTL |
| `owner` | ✗ 不暴露 | ✗ 不暴露 | **保持一致：owner 仅用于过滤，不在响应中暴露** |

### 1.2 字段命名一致性

- `managed_by_sandbox_id`: 与 Sandbox 的 `workspace_id` 形成双向引用
- 时间字段统一使用 `xxx_at` 后缀（`created_at`, `last_accessed_at`）
- ID 字段使用 `xxx_id` 后缀

### 1.3 设计决策点（结合现状）

**Q1: Workspace 是否需要 owner 字段在响应中？**
- **决策**: 不暴露。与 Sandbox API 保持一致，owner 仅用于权限过滤
- **理由**: 减少信息泄露，单租户模式下意义不大

**Q2: 是否需要 `deleted_at` 软删除？**
- **决策**: Workspace 仍保持硬删除；Sandbox 采用软删除（`deleted_at`）作为 tombstone：[`Sandbox.deleted_at`](../../pkgs/bay/app/models/sandbox.py:57)
- **理由**: managed workspace 的清理通过“sandbox 软删 + 内部级联删除 + GC 补偿清理”完成，减少外部 API 状态机复杂度。

## 2. 端点总览（规划接口，当前未实现）

> 说明：下表为设计目标。当前版本尚未暴露 `/v1/workspaces`（见 [0. 实现状态与差异说明](plans/phase-1/workspace-api-design.md:5)）。

| 方法 | 路径 | 描述 | 权限级别 | 当前状态 |
|------|------|------|----------|----------|
| `POST` | `/v1/workspaces` | 创建 external workspace | 标准 | 未实现 |
| `GET` | `/v1/workspaces` | 列出 workspaces | 标准 | 未实现 |
| `GET` | `/v1/workspaces/{id}` | 获取 workspace 详情 | 标准 | 未实现 |
| `DELETE` | `/v1/workspaces/{id}` | 删除 workspace | 标准 | 未实现 |
| `POST` | `/v1/workspaces/{id}/files/read` | 直接读文件 | 高级（Phase 2） | 未实现 |
| `POST` | `/v1/workspaces/{id}/files/write` | 直接写文件 | 高级（Phase 2） | 未实现 |

## 3. 资源模型

### 3.1 Workspace Response（目标形态）

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

对应内部字段来源：[`Workspace`](../../pkgs/bay/app/models/workspace.py:19)

### 3.2 与 Sandbox 的关系（现状一致）

- **managed workspace**: 由 sandbox 创建时隐式创建，生命周期绑定 sandbox（删除时内部级联）
- **external workspace**: 由 `/v1/workspaces` 显式创建（规划），独立于任何 sandbox；当前仍可通过内部 `WorkspaceManager.create(managed=False)` 实现，但未对外暴露

## 4. 端点详细设计（规划接口，当前未实现）

> 以下为 API 设计稿。实现前需要补齐依赖注入与 router（见 [10. 下一步（可选）](plans/phase-1/workspace-api-design.md:391)）。

### 4.1 创建 External Workspace

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

### 4.2 列出 Workspaces

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

> NOTE（实现差异）：当前 [`WorkspaceManager.list()`](../../pkgs/bay/app/managers/workspace/workspace.py:123) 仅支持 owner + cursor/limit，**尚未实现 managed 过滤**。

### 4.3 获取 Workspace 详情

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

### 4.4 删除 Workspace

```
DELETE /v1/workspaces/{id}
```

**Response: 204 No Content**

**目标语义（设计稿）:**
- **external workspace**:
  - 若仍被活跃 sandbox 引用 → 返回 `409 Conflict`
  - 否则直接删除
- **managed workspace**:
  - 设计稿曾提出“若 managing sandbox 已软删则允许删除”，但现状更推荐：**不提供手工删除入口，交给 GC 清理**（见 [8. 场景 2](plans/phase-1/workspace-api-design.md:255)）

**Error Response (409):**
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

> NOTE（实现差异）：当前 [`WorkspaceManager.delete()`](../../pkgs/bay/app/managers/workspace/workspace.py:158) 仅实现了“managed workspace 默认 409（除非 `force=True`）”，尚未实现 external workspace 的“被引用检查”。

## 8. 边界场景分析与推敲（结合现状修订）

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

**✅ 决策: 选项 A - 返回 409 Conflict（未来补齐 /v1/workspaces 时）**

**实现要点（未来）:**
- 在 [`WorkspaceManager.delete()`](../../pkgs/bay/app/managers/workspace/workspace.py:158) 中查询 `Sandbox.workspace_id == target` 且 `Sandbox.deleted_at IS NULL`
  - `Sandbox` 模型：[`Sandbox.workspace_id`](../../pkgs/bay/app/models/sandbox.py:46)
  - 软删字段：[`Sandbox.deleted_at`](../../pkgs/bay/app/models/sandbox.py:57)

### 场景 2: Managed Workspace 的 Sandbox 已软删除

**现状行为（已落地，替代原设计）:**

- `DELETE /v1/sandboxes/{id}` 会把 sandbox 标记为软删：[`SandboxManager.delete()`](../../pkgs/bay/app/managers/sandbox/sandbox.py:383)
- 之后：
  - 若 managed workspace 仍存在（例如中途失败、或历史遗留）
  - 由 GC 任务 [`OrphanWorkspaceGC.run()`](../../pkgs/bay/app/services/gc/tasks/orphan_workspace.py:50) 根据 `sandbox.deleted_at IS NOT NULL` 清理
  - 实际删除动作走内部幂等删除：[`WorkspaceManager.delete_internal_by_id()`](../../pkgs/bay/app/managers/workspace/workspace.py:212)

**结论:**
- 不需要对外暴露“手工删除 managed workspace”的 API 才能达成清理效果。
- 若未来仍要暴露 `/v1/workspaces/{id}` 删除能力，则建议继续保持“managed workspace 由系统处理”，避免用户绕过 sandbox 生命周期管理。

### 场景 3: 并发删除 Sandbox 和 Workspace

> 现状下，由于 `/v1/workspaces` 未暴露，该并发主要发生在“sandbox 删除级联”与“GC 清理”之间。

**潜在问题:**
- GC 与 `SandboxManager.delete()` 可能同时尝试删除同一个 workspace volume

**现有缓解手段（已落地）:**
- `delete_internal_by_id` 明确声明幂等（workspace 不存在则直接返回）：[`WorkspaceManager.delete_internal_by_id()`](../../pkgs/bay/app/managers/workspace/workspace.py:212)
- 对 volume 删除：依赖 driver 的删除操作尽可能幂等；若未来出现稳定性问题，可在 driver 层对 NotFound 做吞掉处理

### 场景 4: 创建 Sandbox 时指定不存在的 workspace_id

**现状:**
- 已通过 [`WorkspaceManager.get()`](../../pkgs/bay/app/managers/workspace/workspace.py:90) 抛出 [`NotFoundError`](../../pkgs/bay/app/errors.py:41)
- 调用路径：[`SandboxManager.create()`](../../pkgs/bay/app/managers/sandbox/sandbox.py:56)

### 场景 5: 创建 Sandbox 时指定别人的 workspace_id

**现状:**
- 同样由 [`WorkspaceManager.get()`](../../pkgs/bay/app/managers/workspace/workspace.py:90) 的 `(Workspace.owner == owner)` 条件实现“404 隐藏权限信息”

### 场景 6: Workspace 列表 - managed 过滤器歧义

- 设计稿中保留 `managed` 过滤。
- 现状下因为 `/v1/workspaces` 未暴露，用户侧不需要此能力；若未来暴露，建议：
  - 默认仅返回 `managed=false`（external）以贴近用户心智
  - 同时保留 `managed` 过滤用于运维排障

## 9. 修订决策汇总（结合现状）

| 主题 | 当前决策 | 说明 |
|------|----------|------|
| managed workspace 的手工删除 | 不提供对外入口（暂时） | 对外 API 未实现；系统通过 sandbox 生命周期 + GC 清理 |
| orphan managed workspace 清理 | GC 负责 | [`OrphanWorkspaceGC`](../../pkgs/bay/app/services/gc/tasks/orphan_workspace.py:23) |
| external workspace 被引用删除保护 | 未来补齐 | 当前 `WorkspaceManager.delete()` 尚未实现引用检查 |
| sandbox create 的 workspace 校验 | 已实现 | 通过 `WorkspaceManager.get()` 的 owner + not found 行为 |

## 10. 需要修改的现有代码（按“当前不冲突/未来补齐”拆分）

### 10.1 当前阶段（不冲突，但需在文档中标注的实现事实）

- `/v1/workspaces` API 路由尚未落地（仅存在内部 manager 能力）
- managed workspace 的清理主要依赖 GC：[`OrphanWorkspaceGC`](../../pkgs/bay/app/services/gc/tasks/orphan_workspace.py:23)

### 10.2 若未来要补齐 `/v1/workspaces`（必须项）

1. **新增路由文件**：[`pkgs/bay/app/api/v1/workspaces.py`](../../pkgs/bay/app/api/v1/workspaces.py:1)（当前不存在）
2. **注册 v1 router**：在 [`pkgs/bay/app/api/v1/__init__.py`](../../pkgs/bay/app/api/v1/__init__.py:1) `include_router(..., prefix="/workspaces")`
3. **增加依赖注入**：在 [`pkgs/bay/app/api/dependencies.py`](../../pkgs/bay/app/api/dependencies.py:43) 增加 `get_workspace_manager()` 及 `WorkspaceManagerDep`
4. **补齐删除保护**：在 [`WorkspaceManager.delete()`](../../pkgs/bay/app/managers/workspace/workspace.py:158) 中对 external workspace 增加“被活跃 sandbox 引用则 409”的检查（见 [场景 1](plans/phase-1/workspace-api-design.md:210)）
5. **补齐 list 过滤**：在 [`WorkspaceManager.list()`](../../pkgs/bay/app/managers/workspace/workspace.py:123) 增加 `managed` 过滤参数（如仍保留该 API 形态）

### 10.3 可选增强（非必须项）

- “允许手工删除 managed workspace（当 managing sandbox 已软删）”
  - 当前通过 GC 达成清理目的；若未来确有运维需求，可考虑在 API 层加 `force` 参数并配合更严格授权。

## 4. 实现计划（更新为：Phase 1 已落地子集 + 后续补齐项）

### 4.1 Phase 1 实际落地（已完成）

- 内部数据模型与 manager：[`Workspace`](../../pkgs/bay/app/models/workspace.py:19)、[`WorkspaceManager`](../../pkgs/bay/app/managers/workspace/workspace.py:23)
- sandbox 与 workspace 的绑定/级联：[`SandboxManager.create()`](../../pkgs/bay/app/managers/sandbox/sandbox.py:56)、[`SandboxManager.delete()`](../../pkgs/bay/app/managers/sandbox/sandbox.py:383)
- orphan managed workspace 的 GC 清理：[`OrphanWorkspaceGC`](../../pkgs/bay/app/services/gc/tasks/orphan_workspace.py:23)

### 4.2 Phase 1（待补齐：对外 /v1/workspaces）

- 新建 [`pkgs/bay/app/api/v1/workspaces.py`](../../pkgs/bay/app/api/v1/workspaces.py:1)
- 更新 [`pkgs/bay/app/api/dependencies.py`](../../pkgs/bay/app/api/dependencies.py:1)
- 更新 [`pkgs/bay/app/api/v1/__init__.py`](../../pkgs/bay/app/api/v1/__init__.py:1)
- 完善 `WorkspaceManager.delete/list` 以满足对外语义

### 4.3 Phase 2（后续扩展）

- `POST /v1/workspaces/{id}/files/read` - 直接读文件
- `POST /v1/workspaces/{id}/files/write` - 直接写文件
- 更严格的权限控制（需要 admin scope）
- 审计日志

## 5. WorkspaceManager 现状核对

现有的 [`WorkspaceManager`](../../pkgs/bay/app/managers/workspace/workspace.py:23) 已实现：
- `create()`: ✓ 已支持 `managed` 参数
- `get()`: ✓ 已有 owner 校验
- `list()`: ✓ 已支持分页（当前不支持 managed 过滤）
- `delete()`: ✓ 已有 managed 保护（managed 默认 409，内部 `force=True` 可绕过）

## 6. 测试策略（若未来补齐 /v1/workspaces）

### 6.1 单元测试

- WorkspaceManager CRUD 操作
- external workspace 删除保护（引用检查）

### 6.2 集成测试

- 创建 external workspace → 绑定 sandbox → 删除 sandbox → workspace 保留
- 创建 sandbox（managed workspace）→ 尝试直接删除 workspace → 409（若暴露 API，则应明确该语义）
- 列出 workspaces 分页与过滤

## 7. 代码结构（规划形态）

```
pkgs/bay/app/
├── api/
│   └── v1/
│       ├── __init__.py          # 添加 workspaces router
│       ├── workspaces.py        # 新建 - Workspace API
│       └── sandboxes.py         # 现有
├── managers/
│   └── workspace/
│       └── workspace.py         # 已有 - 未来可添加 managed 过滤 + 引用删除保护
└── models/
    └── workspace.py             # 已有
```

## 11. 下一步（可选）清单

- 若决定补齐对外 `/v1/workspaces`：按 [10.2](plans/phase-1/workspace-api-design.md:342) 的“必须项”逐条实现与补测试。
- 若决定继续不暴露 `/v1/workspaces`：建议将本文定位为“规划稿/运维草案”，并在 README 或 API 文档中明确对外仅支持 sandbox 面。
