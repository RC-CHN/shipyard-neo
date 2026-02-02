# GC 实现计划评审报告

> 评审对象：[`plans/phase-1.5/gc-implementation-plan.md`](gc-implementation-plan.md)
> 评审日期：2026-02-02

---

## 总体评价

这是一份 **高质量、严谨的技术计划**。文档结构清晰，问题分析透彻，与现有代码的对齐度很高。核心设计决策（如"宁可漏删不可误删"、Driver 抽象层、串行执行策略）都非常合理。

以下是我发现的潜在问题和改进建议：

---

## 🔴 需要修正的问题

### 1. `bay.instance_id` Label 目前并不存在

**问题**：计划 4.4.2 提到 OrphanContainerGC 的强识别条件之一是 `labels["bay.instance_id"] == gc.instance_id`，并在第 254 行备注"Phase 1.5 需要补上 `bay.instance_id`"。

**实际情况**：查看 [`DockerDriver.create()`](../../../pkgs/bay/app/drivers/docker/docker.py:134) 第 148-155 行：

```python
container_labels = {
    "bay.owner": "default",  # TODO: get from session/sandbox
    "bay.sandbox_id": session.sandbox_id,
    "bay.session_id": session.id,
    "bay.workspace_id": cargo.id,
    "bay.profile_id": profile.id,
    "bay.runtime_port": str(runtime_port),
}
```

当前 labels **缺少**：
- `bay.instance_id` ❌
- `bay.managed` ❌

**建议**：在计划的"需要新增/修改"章节明确列出此项变更，并在 todo list 中作为独立任务追踪。

---

### 2. OrphanCargoGC 的 `delete_internal_by_model` 需要考虑 owner 缺失

**问题**：计划 4.3 建议在 [`WorkspaceManager`](../../../pkgs/bay/app/managers/workspace/workspace.py:23) 增加 `delete_internal_by_model(cargo: Cargo)` 方法。

**实际情况**：现有的 [`delete()`](../../../pkgs/bay/app/managers/workspace/workspace.py:158) 方法调用了 `self.get(workspace_id, owner)` 进行 owner 校验。但孤儿 cargo 的 owner 理论上还存在于 `cargo.owner` 字段，只是对应的 sandbox 已被删除。

**潜在问题**：
- 如果 GC 直接传入 `Cargo` 对象，需要确保该对象是从数据库新鲜加载的，而不是 stale 的 detached 对象。
- 建议方法签名改为 `delete_internal_by_id(workspace_id: str) -> None`，内部重新 fetch 后执行删除。

**建议**：
```python
async def delete_internal(self, workspace_id: str) -> None:
    """Internal delete without owner check. For GC / cascade use only."""
    cargo = await self.get_by_id(workspace_id)
    if cargo is None:
        return  # Already deleted, idempotent
    
    # Delete volume first (may fail)
    await self._driver.delete_volume(cargo.driver_ref)
    
    # Delete DB record
    await self._db.delete(cargo)
    await self._db.commit()
```

---

### 3. IdleSessionGC 的竞态防护需要更细致的说明

**问题**：计划 4.1 提到 IdleSessionGC 需要使用 sandbox 级 in-memory lock 与 `ensure_running` 共享。

**实际情况**：查看 [`SandboxManager.ensure_running()`](../../../pkgs/bay/app/managers/sandbox/sandbox.py:211) 第 239-246 行：

```python
# Rollback any pending transaction...
await self._db.rollback()

# Re-fetch sandbox from DB with fresh transaction
result = await self._db.execute(
    select(Sandbox)
    .where(Sandbox.id == sandbox_id)
    .with_for_update()
)
```

**关键细节缺失**：
- 计划提到 "stop/delete 在进入 lock 后也要 rollback 并 refetch"，但没有说明 **IdleSessionGC 本身是否需要这个模式**。
- IdleSessionGC 处理的是"idle_expires_at < now"的 sandbox，理论上此时不应有活跃请求，但竞态仍可能发生（用户恰好在 GC 扫描到之后发起请求）。

**建议**：在计划中明确 IdleSessionGC 的执行流程：

```
1. 获取 sandbox_lock(sandbox_id)
2. async with lock:
   a. rollback + refetch sandbox (with_for_update)
   b. 再次检查 idle_expires_at < now（防止在获取锁期间被 keepalive 刷新）
   c. 如果条件仍满足，执行 destroy sessions + 清理 sandbox 字段
```

---

## 🟡 建议改进

### 4. ExpiredSandboxGC 应增加"二次确认"检查

**现有设计**：
> 对命中的 sandbox：调用 `SandboxManager.delete()`

**潜在风险**：GC 查询到 sandbox.expires_at < now 后，在获取锁并实际执行 delete 之前，用户可能调用了 `extend_ttl` 延长了 TTL。

**建议**：ExpiredSandboxGC 在执行 delete 之前，应在锁内重新检查 `sandbox.expires_at < now`：

```python
async def _process_sandbox(self, sandbox_id: str):
    lock = await get_sandbox_lock(sandbox_id)
    async with lock:
        # Refetch with fresh data
        sandbox = await self._db.execute(
            select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()
        ).scalars().first()
        
        if sandbox is None or sandbox.deleted_at is not None:
            return  # Already deleted
        
        # Double-check expiry (user may have extended TTL)
        if sandbox.expires_at is None or sandbox.expires_at >= datetime.utcnow():
            return  # No longer expired
        
        await self._sandbox_mgr.delete(sandbox)
```

---

### 5. 锁清理策略需要补充

**现有代码**：[`_cleanup_sandbox_lock()`](../../../pkgs/bay/app/managers/sandbox/sandbox.py:55) 在 `SandboxManager.delete()` 末尾调用。

**潜在问题**：
- 如果 delete 过程中失败（如 volume 删除失败），锁不会被清理 → 内存泄漏（轻微）
- 如果 GC 直接调用 `delete()`，锁会被清理；但如果 GC 只调用 `SessionManager.destroy()` + 更新字段（IdleSessionGC 场景），锁不会被清理

**建议**：
1. 在迁移锁到公共模块时，增加定期清理机制（如清理所有对应 `deleted_at IS NOT NULL` 的 sandbox 的锁）
2. 或者在 GC scheduler 每轮结束后调用一次清理

---

### 6. 配置默认值需要再斟酌

**现有设计**：
- `gc.enabled` 默认 **true**
- `gc.run_on_startup` 默认 **true**

**潜在风险**：对于现有部署（upgrade 场景），突然启用 GC 可能导致意外行为（尤其是 OrphanContainerGC 如果 label 配置不对）。

**建议**：
- Phase 1.5 初版 `gc.enabled` 默认 **false**，发布后观察一段时间再改为 true
- 或者只启用"安全的" GC 任务（IdleSessionGC、ExpiredSandboxGC），OrphanContainerGC 默认关闭

```yaml
gc:
  enabled: true
  tasks:
    idle_session:
      enabled: true
    expired_sandbox:
      enabled: true
    orphan_workspace:
      enabled: true
    orphan_container:
      enabled: false  # Requires explicit opt-in due to strict safety requirements
```

---

### 7. Driver 新接口的命名与返回值

**现有设计**：
```python
list_runtime_instances(*, labels: dict[str, str]) -> list[RuntimeInstance]
destroy_runtime_instance(instance_id: str) -> None
```

**建议**：
1. `list_runtime_instances` 应返回 `AsyncIterator[RuntimeInstance]` 或支持分页，避免在容器数量很大时一次性加载全部到内存
2. `RuntimeInstance` 应包含 `state` 字段（running/stopped/etc），以便 GC 可选择只处理 running 状态的孤儿

```python
@dataclass
class RuntimeInstance:
    id: str
    name: str
    labels: dict[str, str]
    state: str  # "running", "exited", etc.
    created_at: datetime | None = None
```

---

### 8. 测试覆盖需要补充边界场景

**现有测试计划**（7.2）只覆盖了 happy path。

**建议增加**：
1. **竞态测试**：同时触发 GC 和 `ensure_running`，验证锁机制有效
2. **部分失败测试**：模拟 volume 删除失败，验证 DB 记录保留
3. **空跑测试**：没有任何符合条件的资源时，GC 正常完成
4. **重入测试**：连续调用两次 `run_once()`，验证不会重复处理

---

## 🟢 亮点确认

以下设计决策非常好，建议保留：

1. ✅ **宁可漏删不可误删** - 在多容器混跑环境中至关重要
2. ✅ **Driver 抽象层保留** - 为 Phase 2 K8s 做好准备
3. ✅ **单循环串行执行** - 简化竞态处理，日志易读
4. ✅ **NoopCoordinator 预留** - 为多实例部署提供扩展点
5. ✅ **Phase 1.5 不做 dry-run** - 避免功能蔓延，依赖 strict 门槛
6. ✅ **锁模块独立** - `concurrency/locks.py` 分层清晰

---

## 建议的实施优先级调整

原计划的任务顺序建议调整为：

1. **Labels 补全**（前置条件）
   - 在 `DockerDriver.create()` 添加 `bay.instance_id` 和 `bay.managed=true`

2. **公共锁模块**
   - 创建 `concurrency/locks.py`
   - 迁移 `_get_sandbox_lock` / `_cleanup_sandbox_lock`

3. **GC 框架骨架**
   - `services/gc/base.py` - 接口定义
   - `services/gc/scheduler.py` - 调度器
   - `services/gc/coordinator.py` - NoopCoordinator

4. **配置扩展**
   - `config.py` 添加 GC 配置
   - `config.yaml.example` 添加示例

5. **Driver 扩展**
   - `list_runtime_instances()`
   - `destroy_runtime_instance()`

6. **四个 GC 任务实现**（按顺序）
   - IdleSessionGC
   - ExpiredSandboxGC
   - OrphanCargoGC
   - OrphanContainerGC

7. **FastAPI lifespan 集成**

8. **测试**

---

## 结论

这份计划总体上是 **可执行的**，只需要补充上述细节。建议在开始编码前：

1. ✅ 确认 Label 补全是否需要数据迁移（**不需要，服务尚未上线**）
2. ✅ 决定 `gc.enabled` 的默认值策略（**默认 true**）
3. ⏳ 明确 IdleSessionGC 和 ExpiredSandboxGC 的"锁内二次确认"模式（见下文分析）

---

## 补充：关于"锁内二次确认"的取舍分析

### 场景分析

**竞态场景**（如果不做二次确认）：

```
时间线：
T0: GC 扫描，发现 sandbox-A 的 idle_expires_at < now
T1: 用户发起请求 → ensure_running 被调用 → 更新 idle_expires_at = now + 30min
T2: GC 开始处理 sandbox-A，获取锁
T3: GC 执行 destroy sessions（用户刚激活的 sandbox 被回收！）
```

### 如果放弃二次确认，会发生什么？

| 场景 | 后果 | 严重程度 | 发生概率 |
|------|------|----------|----------|
| IdleSessionGC | 用户恰好在 GC 扫描后、执行前激活了 sandbox，session 被意外销毁 | ⚠️ 中 | 🔵 低 |
| ExpiredSandboxGC | 用户恰好在 GC 扫描后调用 `extend_ttl`，sandbox 仍被删除 | 🔴 高 | 🔵 低 |

**详细分析**：

1. **IdleSessionGC 放弃二次确认**：
   - **后果**：用户刚发起的请求会收到 503 或连接错误，因为 session 被销毁了
   - **恢复**：下次请求会触发 `ensure_running` 创建新 session，**功能可恢复**
   - **用户感知**：一次请求失败，需要重试
   - **评估**：**可以接受**，因为恢复成本低

2. **ExpiredSandboxGC 放弃二次确认**：
   - **后果**：用户明确调用了 `extend_ttl`（表明他们还想用这个 sandbox），但 GC 仍然删除了它
   - **恢复**：sandbox 被软删除，**无法恢复**，用户需要创建新的
   - **用户感知**：花钱延期的 sandbox 被删了，数据丢失
   - **评估**：**不建议放弃**，因为这违反了用户的明确意图

### 结论与建议

| GC 任务 | 二次确认 | 理由 |
|---------|---------|------|
| **IdleSessionGC** | ❌ 可以放弃 | 即使误回收，用户下次请求自动恢复；竞态窗口极短 |
| **ExpiredSandboxGC** | ✅ 建议保留 | 用户调用 `extend_ttl` 是明确的续期意图，删除它会造成不可逆数据丢失 |
| **OrphanCargoGC** | ❌ 不需要 | sandbox 一旦 deleted_at 设置后不会复活 |
| **OrphanContainerGC** | ❌ 不需要 | Session 被硬删后不会复活 |

**简化实现建议**：

```python
# IdleSessionGC - 简化版，不做二次确认
async def _process_idle_sandbox(self, sandbox_id: str):
    lock = await get_sandbox_lock(sandbox_id)
    async with lock:
        await self._db.rollback()
        sandbox = await self._fetch_sandbox(sandbox_id)
        if sandbox is None or sandbox.deleted_at is not None:
            return
        # 不检查 idle_expires_at，直接执行
        # 最坏情况：用户刚激活的 session 被销毁，下次请求自动恢复
        await self._destroy_all_sessions(sandbox)
        sandbox.current_session_id = None
        sandbox.idle_expires_at = None
        await self._db.commit()

# ExpiredSandboxGC - 保留二次确认
async def _process_expired_sandbox(self, sandbox_id: str):
    lock = await get_sandbox_lock(sandbox_id)
    async with lock:
        await self._db.rollback()
        sandbox = await self._fetch_sandbox(sandbox_id)
        if sandbox is None or sandbox.deleted_at is not None:
            return
        
        # 二次确认：用户可能调用了 extend_ttl
        if sandbox.expires_at is None or sandbox.expires_at >= datetime.utcnow():
            return  # 不再过期，跳过
        
        await self._sandbox_mgr.delete(sandbox)
```

**代码复杂度影响**：二次确认只是多一个 if 判断，增加 ~3 行代码，几乎没有额外复杂度。

---

## 最终建议

- **IdleSessionGC**：保留二次确认（防止用户刚激活的 session 被删，虽然可恢复但体验更好）✅
- **ExpiredSandboxGC**：保留二次确认（防止用户刚 extend_ttl 的 sandbox 被删）✅
- **其他两个**：不需要二次确认

这样统一了模式，保护了用户明确意图的操作。

---

## 9. 详细实施计划 (Todo List)

### Phase 1: 基础建设

- [ ] **1.1 Docker Driver 增强**
  - 修改 `pkgs/bay/app/drivers/docker/docker.py` 的 `create` 方法，添加 `bay.instance_id` (从配置取) 和 `bay.managed=true` 到容器 Labels。
  - 在 `pkgs/bay/app/drivers/base.py` 中定义 `RuntimeInstance` dataclass 和抽象方法 `list_runtime_instances`, `destroy_runtime_instance`。
  - 在 `pkgs/bay/app/drivers/docker/docker.py` 中实现上述两个方法。

- [ ] **1.2 公共锁模块提取**
  - 创建 `pkgs/bay/app/concurrency/locks.py`。
  - 将 `pkgs/bay/app/managers/sandbox/sandbox.py` 中的 `_get_sandbox_lock` 和 `_cleanup_sandbox_lock` 逻辑迁移过去。
  - 更新 `SandboxManager` 使用新的锁模块。

- [ ] **1.3 配置扩展**
  - 在 `pkgs/bay/app/config.py` 中添加 `GCConfig` 和 `GCTaskConfig` 类。
  - 更新 `Settings` 类包含 `gc` 配置。
  - 更新 `pkgs/bay/config.yaml.example`。

### Phase 2: GC 框架与任务实现

- [ ] **2.1 GC 核心框架**
  - 创建 `pkgs/bay/app/services/gc/` 目录。
  - 实现 `pkgs/bay/app/services/gc/base.py` (GCTask 抽象基类, GCResult)。
  - 实现 `pkgs/bay/app/services/gc/coordinator.py` (NoopCoordinator)。
  - 实现 `pkgs/bay/app/services/gc/scheduler.py` (GCScheduler, 负责串行执行任务)。

- [ ] **2.2 实现 GC 任务**
  - 实现 `pkgs/bay/app/services/gc/tasks/idle_session.py` (实现 IdleSessionGC，**保留**二次确认)。
  - 实现 `pkgs/bay/app/services/gc/tasks/expired_sandbox.py` (实现 ExpiredSandboxGC，**保留**二次确认)。
  - 在 `pkgs/bay/app/managers/workspace/workspace.py` 添加 `delete_internal_by_id`。
  - 实现 `pkgs/bay/app/services/gc/tasks/orphan_workspace.py`。
  - 实现 `pkgs/bay/app/services/gc/tasks/orphan_container.py` (Strict 模式)。

### Phase 3: 集成与测试

- [ ] **3.1 系统集成**
  - 在 `pkgs/bay/app/main.py` 的 `lifespan` 中初始化 `GCScheduler`。
  - 启动时执行 `run_once`，随后启动后台循环。
  - 确保 shutdown 时优雅停止。

- [ ] **3.2 测试验证**
  - 编写 Unit Tests: `pkgs/bay/tests/unit/test_gc_scheduler.py`。
  - 编写 Integration Tests: `pkgs/bay/tests/integration/test_gc_e2e.py` (覆盖 Idle回收, 过期清理, 孤儿容器防误删)。

---

## 10. 代码骨架参考

### 10.1 目录结构

```text
pkgs/bay/app/
├── concurrency/
│   ├── __init__.py
│   └── locks.py          <-- New: Sandbox locks
├── services/
│   ├── gc/
│   │   ├── __init__.py
│   │   ├── base.py       <-- New: Task interface
│   │   ├── coordinator.py <-- New: Coordination
│   │   ├── scheduler.py  <-- New: Main loop
│   │   └── tasks/
│   │       ├── __init__.py
│   │       ├── idle_session.py
│   │       ├── expired_sandbox.py
│   │       ├── orphan_workspace.py
│   │       └── orphan_container.py
```

### 10.2 Driver Interface (`pkgs/bay/app/drivers/base.py`)

```python
@dataclass
class RuntimeInstance:
    id: str
    name: str
    labels: dict[str, str]
    state: str  # "running", "exited", etc.
    created_at: datetime | None = None

class Driver(ABC):
    # ... existing methods ...

    @abstractmethod
    async def list_runtime_instances(self, *, labels: dict[str, str]) -> list[RuntimeInstance]:
        """List runtime instances matching labels."""
        ...

    @abstractmethod
    async def destroy_runtime_instance(self, instance_id: str) -> None:
        """Force destroy a runtime instance."""
        ...
```

### 10.3 GC Task Interface (`pkgs/bay/app/services/gc/base.py`)

```python
@dataclass
class GCResult:
    cleaned_count: int = 0
    errors: list[str] = field(default_factory=list)

class GCTask(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def run(self) -> GCResult: ...
```

