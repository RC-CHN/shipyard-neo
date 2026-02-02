# Phase 1.5 GC Implementation Path

> **Status**: Approved
> **Phase**: 1.5 (Core GC)
> **Goal**: Implement background Garbage Collection for Idle Sessions, Expired Sandboxes, Orphan Workspaces, and Orphan Containers.

---

## 1. 核心策略确认 (Policy)

| 策略项 | 设定值 | 说明 |
|---|---|---|
| **默认启用** | `gc.enabled = True` | 开箱即用 |
| **启动时运行** | `gc.run_on_startup = True` | 确保重启后立即清理残留 |
| **孤儿容器清理** | `orphan_container.enabled = False` | **默认关闭**，需显式开启 (Strict Mode 防误删) |
| **锁内二次确认** | **Required** | IdleSessionGC 和 ExpiredSandboxGC 必须在获取锁后重新检查时间戳 |
| **执行模式** | **Serial** | 单线程串行执行 4 个任务，减少并发风险 |
| **多实例支持** | **Safe-fail** | 依赖 DB 状态原子性和 Label 强校验；暂无分布式锁 (Phase 2) |

---

## 2. 实施步骤 (Execution Steps)

### Step 1: 基础建设 (Infrastructure)

#### 1.1 补全容器 Labels (Critical Pre-requisite)
**Target**: `pkgs/bay/app/drivers/docker/docker.py`
- 修改 `create()` 方法，在 `container_labels` 中增加：
  - `bay.instance_id`: 取自 `settings.gc.instance_id`
  - `bay.managed`: 固定为 `"true"`
- **Why**: OrphanContainerGC 的 Strict 模式依赖这些 Labels 进行安全识别。

#### 1.2 统一并发锁 (Concurrency)
**Target**: `pkgs/bay/app/concurrency/locks.py` (New), `pkgs/bay/app/managers/sandbox/sandbox.py`
- 创建 `pkgs/bay/app/concurrency/locks.py`：
  - 迁移 `_get_sandbox_lock` 和 `_cleanup_sandbox_lock`。
- 修改 `SandboxManager`：
  - `ensure_running`: 使用新的锁模块。
  - `stop`: 增加锁保护 + rollback/refetch。
  - `delete`: 增加锁保护 + rollback/refetch。
- **Why**: 确保 GC 与用户请求（ensure_running/stop/delete）在同一把锁下互斥，防止并发状态损坏。

#### 1.3 配置扩展 (Configuration)
**Target**: `pkgs/bay/app/config.py`, `pkgs/bay/config.yaml.example`
- 在 `Settings` 中增加 `GCConfig`:
  ```python
  class GCTaskConfig(BaseModel):
      enabled: bool = True

  class GCConfig(BaseModel):
      enabled: bool = True
      run_on_startup: bool = True
      interval_seconds: int = 300
      instance_id: str = "bay"  # Default
      tasks: dict[str, GCTaskConfig]
  ```
- 更新 `config.yaml.example`。

#### 1.4 Driver 抽象扩展 (Driver API)
**Target**: `pkgs/bay/app/drivers/base.py`, `pkgs/bay/app/drivers/docker/docker.py`
- 在 `Driver` 基类增加：
  - `RuntimeInstance` (dataclass)
  - `list_runtime_instances(labels: dict) -> list[RuntimeInstance]`
  - `destroy_runtime_instance(instance_id: str) -> None`
- 在 `DockerDriver` 中实现上述方法。
- **Why**: 解耦 GC 与具体运行时实现（为 K8s 铺路）。

#### 1.5 Cargo 内部删除 API
**Target**: `pkgs/bay/app/managers/workspace/workspace.py`
- 增加 `delete_internal_by_id(workspace_id: str) -> None`。
- 逻辑：跳过 owner 校验，直接执行 delete volume + delete db record。
- **Why**: GC 运行在系统上下文，无 owner 信息，且需清理所有人的孤儿资源。

---

### Step 2: GC 框架与任务实现 (Implementation)

#### 2.1 GC 核心框架
**Target**: `pkgs/bay/app/services/gc/`
- `base.py`: 定义 `GCTask` 抽象基类, `GCResult`。
- `coordinator.py`: 实现 `NoopCoordinator` (总是允许执行)。
- `scheduler.py`: 实现 `GCScheduler`。
  - `start()`, `stop()`, `run_once()`
  - 内部 `while` 循环串行执行所有 enabled tasks。

#### 2.2 任务实现 (Tasks)
**Target**: `pkgs/bay/app/services/gc/tasks/`

1.  **`IdleSessionGC`**
    - **Trigger**: `idle_expires_at < now` AND `deleted_at is null`
    - **Action**:
      - Acquire Lock -> Rollback -> Refetch
      - **Check**: `idle_expires_at < now` (Double Check)
      - Destroy sessions (Driver + DB)
      - Update Sandbox: `current_session_id = None`, `idle_expires_at = None`

2.  **`ExpiredSandboxGC`**
    - **Trigger**: `expires_at < now` AND `deleted_at is null`
    - **Action**:
      - Acquire Lock -> Rollback -> Refetch
      - **Check**: `expires_at < now` (Double Check)
      - Call `SandboxManager.delete(sandbox)`

3.  **`OrphanCargoGC`**
    - **Trigger**: `managed=True` AND (`managed_by_sandbox_id is null` OR `sandbox.deleted_at is not null`)
    - **Action**: Call `WorkspaceManager.delete_internal_by_id()`

4.  **`OrphanContainerGC`** (Strict Mode)
    - **Config**: Default Disabled.
    - **Discovery**: `driver.list_runtime_instances` with labels:
      - `bay.managed=true`
      - `bay.instance_id={gc.instance_id}`
    - **Verification**: Check if `bay.session_id` exists in DB.
    - **Action**: If not in DB -> `driver.destroy_runtime_instance`.

---

### Step 3: 集成与验收 (Integration)

#### 3.1 生命周期集成
**Target**: `pkgs/bay/app/main.py`
- 在 `lifespan` 中：
  - Init `GCScheduler`.
  - If `gc.run_on_startup`: `await scheduler.run_once()`.
  - If `gc.enabled`: `await scheduler.start()`.
  - Shutdown: `await scheduler.stop()`.

#### 3.2 测试计划
- **Unit Tests**:
  - Scheduler 逻辑 (串行执行、异常捕获)。
  - Task 查询条件验证 (Mock DB)。
- **Integration Tests** (`pkgs/bay/tests/integration/`):
  - `test_gc_idle.py`: 制造过期 idle session -> 触发 GC -> 验证 session 销毁 & sandbox 状态。
  - `test_gc_expired.py`: 制造过期 sandbox -> 触发 GC -> 验证软删除。
  - `test_gc_orphan_container.py`:
    - Case 1: 完美匹配 Labels 但无 DB 记录 -> 被删。
    - Case 2: Labels 不匹配 / instance_id 不对 -> **不被删** (Safe)。
