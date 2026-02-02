# Admin GC API 设计

> 状态: 🔄 待评审
> 更新日期: 2026-02-02

## 1. 背景与动机

当前 E2E 测试中的 GC 相关测试（如 [`test_gc_e2e.py`](../../pkgs/bay/tests/integration/test_gc_e2e.py:1)）存在以下问题：

1. **依赖时序**：测试依赖 `gc.interval_seconds` 配置，需要等待 GC 自动执行
2. **不稳定性**：时序依赖导致测试容易因时间窗口问题而失败
3. **效率低**：需要设置很短的 interval（如 5s）来减少等待，但仍然浪费时间
4. **调试困难**：无法按需触发 GC 来验证行为

**解决方案**：提供 Admin API 用于手动触发 GC，测试可以：
- 关闭自动 GC（`gc.enabled: false` 或 `interval_seconds: 9999`）
- 在需要时通过 API 手动触发 GC 并等待完成
- 获得确定性的测试行为

## 2. API 设计

### 2.1 端点定义

```
POST /admin/gc/run
```

**功能**：立即触发一次完整的 GC 循环，**同步执行**并等待完成。

**请求体**（可选）：
```json
{
  "tasks": ["idle_session", "expired_sandbox", "orphan_workspace", "orphan_container"]
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tasks` | `string[]` | `null` | 指定要运行的任务；`null` 表示运行所有已启用的任务 |

**响应**：
```json
{
  "results": [
    {
      "task_name": "idle_session",
      "cleaned_count": 2,
      "skipped_count": 0,
      "errors": []
    },
    {
      "task_name": "expired_sandbox",
      "cleaned_count": 1,
      "skipped_count": 0,
      "errors": []
    }
  ],
  "total_cleaned": 3,
  "total_errors": 0,
  "duration_ms": 245
}
```

**状态码**：
| 状态码 | 说明 |
|--------|------|
| 200 | GC 执行成功（即使部分任务有错误） |
| 423 | GC 正在执行中（重入保护） |
| 503 | GC 未启用或 scheduler 不可用 |

### 2.2 可选：状态查询端点

```
GET /admin/gc/status
```

**响应**：
```json
{
  "enabled": true,
  "is_running": false,
  "instance_id": "bay-e2e",
  "interval_seconds": 300,
  "tasks": {
    "idle_session": { "enabled": true },
    "expired_sandbox": { "enabled": true },
    "orphan_workspace": { "enabled": true },
    "orphan_container": { "enabled": false }
  },
  "last_run_at": "2026-02-02T10:30:00Z",
  "last_run_results": { ... }
}
```

## 3. 安全考虑

### 3.1 认证要求

Admin API 应该受到与普通 API 相同的认证保护，且可选增加额外限制：

| 方案 | 说明 | 推荐 |
|------|------|------|
| **A. 共用 API Key** | 使用现有的 `security.api_key` | ✅ 简单，适合 Phase 1.5 |
| B. 独立 Admin Key | 增加 `security.admin_api_key` | 🔄 可选扩展 |
| C. IP 白名单 | 限制 Admin API 的源 IP | 🔄 可选扩展 |

**Phase 1.5 建议**：使用方案 A（共用 API Key），测试环境下已有 `e2e-test-api-key`。

### 3.2 重入保护

Admin GC API 复用 [`GCScheduler._run_lock`](../../pkgs/bay/app/services/gc/scheduler.py:67)，确保：
- 同一时刻只有一个 GC 循环在执行
- 如果后台 loop 正在执行，Admin API 返回 423 而不是阻塞

```python
# 选项 A: 直接返回错误（推荐）
if self._run_lock.locked():
    raise HTTPException(423, "GC is already running")

# 选项 B: 等待锁
async with self._run_lock:
    ...
```

## 4. 实现方案

### 4.1 API 路由

**新增文件**：`pkgs/bay/app/api/v1/admin.py`

```python
"""Admin API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.dependencies import require_authenticated
from app.services.gc.lifecycle import get_gc_scheduler

router = APIRouter(prefix="/admin", tags=["admin"])


class GCRunRequest(BaseModel):
    """Request body for manual GC trigger."""
    tasks: list[str] | None = None  # None = all enabled tasks


class GCTaskResult(BaseModel):
    """Result of a single GC task."""
    task_name: str
    cleaned_count: int
    skipped_count: int
    errors: list[str]


class GCRunResponse(BaseModel):
    """Response from manual GC run."""
    results: list[GCTaskResult]
    total_cleaned: int
    total_errors: int
    duration_ms: int


@router.post("/gc/run", response_model=GCRunResponse)
async def run_gc(
    request: GCRunRequest | None = None,
    _auth: None = Depends(require_authenticated),
) -> GCRunResponse:
    """Manually trigger a GC cycle.
    
    This endpoint runs GC synchronously and waits for completion.
    Returns detailed results for each GC task.
    
    Use this in tests instead of relying on automatic GC timing.
    """
    scheduler = get_gc_scheduler()
    
    if scheduler is None:
        raise HTTPException(503, detail="GC is not enabled")
    
    # Check if already running (non-blocking)
    if scheduler._run_lock.locked():
        raise HTTPException(423, detail="GC is already running")
    
    import time
    start = time.monotonic()
    
    # Run GC cycle
    results = await scheduler.run_once()
    
    duration_ms = int((time.monotonic() - start) * 1000)
    
    return GCRunResponse(
        results=[
            GCTaskResult(
                task_name=r.task_name,
                cleaned_count=r.cleaned_count,
                skipped_count=r.skipped_count,
                errors=r.errors,
            )
            for r in results
        ],
        total_cleaned=sum(r.cleaned_count for r in results),
        total_errors=sum(len(r.errors) for r in results),
        duration_ms=duration_ms,
    )
```

### 4.2 路由注册

**修改**：`pkgs/bay/app/api/v1/__init__.py`

```python
from app.api.v1 import admin  # 新增

router.include_router(admin.router)
```

### 4.3 Scheduler 扩展（可选）

如果需要支持按任务过滤，可在 [`GCScheduler`](../../pkgs/bay/app/services/gc/scheduler.py:19) 增加：

```python
async def run_once(self, *, tasks: list[str] | None = None) -> list[GCResult]:
    """Execute one GC cycle.
    
    Args:
        tasks: Optional list of task names to run. None = all tasks.
    """
    async with self._run_lock:
        return await self._run_cycle(tasks=tasks)

async def _run_cycle(self, *, tasks: list[str] | None = None) -> list[GCResult]:
    ...
    for task in self._tasks:
        if tasks is not None and task.name not in tasks:
            continue
        result = await self._run_task(task)
        ...
```

## 5. 测试改进

### 5.1 测试配置调整

**修改**：`pkgs/bay/tests/scripts/docker-host/config.yaml`

```yaml
gc:
  enabled: true  # 需要启用才能使用 Admin API
  run_on_startup: false
  interval_seconds: 86400  # 24 小时，实际上禁用自动 GC
  
  # 任务配置保持不变
  idle_session:
    enabled: true
  expired_sandbox:
    enabled: true
  ...
```

### 5.2 测试辅助函数

**新增**：`pkgs/bay/tests/integration/conftest.py`

```python
async def trigger_gc(client: httpx.AsyncClient) -> dict:
    """Trigger GC manually and wait for completion."""
    response = await client.post("/admin/gc/run")
    assert response.status_code == 200, f"GC failed: {response.text}"
    return response.json()


async def trigger_gc_task(client: httpx.AsyncClient, task: str) -> dict:
    """Trigger a specific GC task."""
    response = await client.post(
        "/admin/gc/run",
        json={"tasks": [task]},
    )
    assert response.status_code == 200, f"GC failed: {response.text}"
    return response.json()
```

### 5.3 测试示例（改进后）

```python
async def test_expired_sandbox_gc_deletes_sandbox(self):
    async with httpx.AsyncClient(...) as client:
        # Create sandbox with very short TTL
        sandbox = await create_sandbox(client, ttl=1)
        sandbox_id = sandbox["id"]
        
        # Wait for TTL to expire
        await asyncio.sleep(1.2)
        
        # Verify sandbox is still visible (status=EXPIRED) before GC
        r = await client.get(f"/v1/sandboxes/{sandbox_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "expired"
        
        # Trigger GC manually
        gc_result = await trigger_gc_task(client, "expired_sandbox")
        assert gc_result["total_cleaned"] >= 1
        
        # Verify sandbox is deleted
        r = await client.get(f"/v1/sandboxes/{sandbox_id}")
        assert r.status_code == 404
```

## 6. 实现步骤

- [ ] **1. 创建 Admin API 模块**
  - 新增 `pkgs/bay/app/api/v1/admin.py`
  - 实现 `POST /admin/gc/run` 端点
  
- [ ] **2. 注册路由**
  - 修改 `pkgs/bay/app/api/v1/__init__.py`
  
- [ ] **3. 更新测试配置**
  - 修改 `pkgs/bay/tests/scripts/docker-host/config.yaml`
  - 将 `interval_seconds` 改为较大值
  
- [ ] **4. 添加测试辅助函数**
  - 在 `conftest.py` 添加 `trigger_gc()` 和 `trigger_gc_task()`
  
- [ ] **5. 重构 GC E2E 测试**
  - 修改 `test_gc_e2e.py` 使用 Admin API 而非等待自动 GC
  
- [ ] **6. 添加 Admin API 测试**
  - 新增 `test_admin_gc.py` 验证 Admin API 本身的行为

## 7. 备选方案

### 7.1 方案 B：使用 DB 直接操作（不推荐）

让测试直接操作数据库（设置 `expires_at` 到过去），然后调用内部函数触发 GC。

**缺点**：
- 破坏封装，测试与内部实现耦合
- 无法测试真实的 API 流程

### 7.2 方案 C：WebSocket 通知（过度设计）

GC 完成后通过 WebSocket 通知客户端。

**缺点**：
- 复杂度高
- Phase 1.5 不需要这种实时性

## 8. 与现有代码的对齐

| 组件 | 现有接口 | Admin API 使用方式 |
|------|----------|-------------------|
| [`GCScheduler.run_once()`](../../pkgs/bay/app/services/gc/scheduler.py:74) | 已实现 | 直接调用 |
| [`get_gc_scheduler()`](../../pkgs/bay/app/services/gc/lifecycle.py:185) | 已实现 | 获取全局实例 |
| [`GCScheduler._run_lock`](../../pkgs/bay/app/services/gc/scheduler.py:67) | 已实现 | 检查重入状态 |
| [`GCResult`](../../pkgs/bay/app/services/gc/base.py) | 已实现 | 转换为响应 |

Admin API 不需要修改现有 GC 核心逻辑，仅增加一个 HTTP 入口。

---

## 9. 结论

推荐采用 **Admin GC API 方案**，原因：

1. **简单**：仅增加一个 API 端点，复用现有 `run_once()` 逻辑
2. **解耦**：测试不依赖时序，变得确定性和稳定
3. **可调试**：开发时可通过 curl 手动触发 GC 验证行为
4. **低风险**：Admin API 受认证保护，不影响生产安全性

实现工作量预估：约 100-150 行代码（含测试）。
