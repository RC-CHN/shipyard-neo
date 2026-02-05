# Shipyard-Neo 代码审查清单

> **审查者视角**: Linus Torvalds 风格的代码质量审查
>
> **核心原则**:
> - "好品味"是让特殊情况消失、数据结构正确、代码简洁清晰
> - **轻量化优先**：拒绝引入不必要的重型依赖，用最简单的方案解决实际问题
> - "Theory loses. Every single time." — 解决真实问题，不做过度设计
>
> **最后更新**: 2026-02-05

---

## 总体评价

这个项目架构清晰，分层合理（Bay 编排层 + Ship 运行时）。数据模型设计体现了"计算与存储分离"的核心理念。项目已经很好地保持了轻量化——仅依赖 FastAPI + SQLite/SQLModel + Docker，没有引入 Redis/etcd 等分布式组件。

以下审查建议均遵循**轻量化原则**，优先使用现有工具和简单方案。

---

## 🔴 高优先级审查项

### 1. ✅ 并发控制与锁机制（已改进）

**文件**: [`pkgs/bay/app/concurrency/locks.py`](pkgs/bay/app/concurrency/locks.py)

**当前状态**: 已重构为独立模块，提供更好的锁管理。

```python
# 新实现 - 独立的锁模块
async def get_sandbox_lock(sandbox_id: str) -> asyncio.Lock
async def cleanup_sandbox_lock(sandbox_id: str) -> None
async def cleanup_deleted_sandbox_locks(active_sandbox_ids: set[str]) -> None
```

**已解决**:
- [x] 锁逻辑从 SandboxManager 抽离到独立模块
- [x] 提供 `cleanup_deleted_sandbox_locks()` 清理已删除 sandbox 的锁
- [x] SQLite stale read 问题已修复（`fix(bay): resolve SQLite stale read issues`）

**待改进**:
- [ ] 多实例部署仍需依赖数据库乐观锁
- [ ] 锁超时机制（避免死锁）

---

### 2. ✅ 路径安全校验（已实现）

**文件**: [`pkgs/bay/app/validators/path.py`](pkgs/bay/app/validators/path.py)

**当前状态**: Bay 侧已实现完整的路径校验，与 Ship 形成双层防护。

```python
def validate_relative_path(path: str) -> str:
    """校验相对路径，拒绝绝对路径和目录穿越"""

def validate_optional_relative_path(path: str | None) -> str | None:
    """可选路径校验"""
```

**已解决**:
- [x] Bay 侧路径校验实现（禁止绝对路径、目录穿越）
- [x] 与 Ship `resolve_path` 对齐
- [x] 单元测试覆盖 (`test_path_validator.py`)
- [x] E2E 测试覆盖 (`test_path_security.py`)

---

### 3. ✅ 异常处理与资源泄露（已修复：Session 启动失败清理）

**文件**: [`pkgs/bay/app/managers/session/session.py`](pkgs/bay/app/managers/session/session.py:110)

**修复摘要**:
- endpoint 只在 runtime `/health` readiness 成功后才落库
- 失败路径 best-effort `driver.destroy(container_id)`，并清空 `container_id/endpoint` 后标记 `FAILED`
- readiness timeout 的 `SessionNotReadyError.details["sandbox_id"]` 修正为真实 `sandbox_id`

```python
container_id = session.container_id or await self._driver.create(...)
try:
    endpoint = await self._driver.start(container_id, runtime_port=...)
    await self._wait_for_ready(endpoint, sandbox_id=session.sandbox_id, session_id=session.id)
    session.endpoint = endpoint  # only after ready
    session.observed_state = SessionStatus.RUNNING
except Exception:
    await self._driver.destroy(container_id)  # best-effort
    session.container_id = None
    session.endpoint = None
    session.observed_state = SessionStatus.FAILED
    raise
```

**已解决**:
- [x] `driver.start`/readiness 失败时不会留下孤儿容器
- [x] 不会持久化不可用的 `session.endpoint`（避免脏写）
- [x] 错误元数据 `sandbox_id` 正确
- [x] 单元测试覆盖：`pkgs/bay/tests/unit/managers/test_session_manager.py`

---

### 4. ⏸️ 时间相关的竞态条件（暂不处理）

**文件**: [`pkgs/bay/app/models/sandbox.py`](pkgs/bay/app/models/sandbox.py:77)

**问题**: `is_expired` 属性每次调用都重新计算 `datetime.utcnow()`：

```python
@property
def is_expired(self) -> bool:
    if self.expires_at is None:
        return False
    return datetime.utcnow() > self.expires_at  # 每次调用时间不同
```

**审查要点**:
- [ ] 同一个请求处理流程中多次检查 `is_expired` 可能得到不同结果
- [ ] [`compute_status()`](pkgs/bay/app/models/sandbox.py:83) 调用 `is_expired`，可能导致状态不一致
- [ ] 考虑在请求上下文中固定时间基准

**2026-02-05 分析结论：暂不处理**

调用链分析：
- `is_expired` 只在 `compute_status()` 内部调用
- `compute_status()` 只在 `_sandbox_to_response()` 中调用（构造 API 响应）
- 同一请求只调用一次 `compute_status()`，不存在多次检查不一致问题

实际影响极低：
- ✅ 只影响 API 返回的 `status` 字段展示，不影响业务逻辑
- ✅ `extend_ttl()` 中的过期判断是独立计算的
- ✅ GC 清理过期 sandbox 也是独立判断的

---

### 5. ✅ httpx 客户端连接管理（已实现连接池）

**文件**:
- [`pkgs/bay/app/services/http/client.py`](pkgs/bay/app/services/http/client.py)
- [`pkgs/bay/app/main.py`](pkgs/bay/app/main.py)
- [`pkgs/bay/app/adapters/ship.py`](pkgs/bay/app/adapters/ship.py)
- [`pkgs/bay/app/managers/session/session.py`](pkgs/bay/app/managers/session/session.py)

**当前状态**: 已落地“共享 httpx.AsyncClient + FastAPI lifespan 管理”的轻量化方案。

```python
# 共享连接池（示意）
client = http_client_manager.client
response = await client.request(...)
```

**已解决**:
- [x] `HTTPClientManager` 统一管理连接池与生命周期
- [x] `ShipAdapter` 与 runtime `/health` readiness polling 复用共享 client（测试场景有 fallback）
- [x] `trust_env=False`，避免环境代理导致 Docker 私网 IP 访问异常（例如返回 502）

**待改进**:
- [x] 失败路径资源清理已补强（见第 3 条：Session 启动失败清理）

---

## 🟠 中优先级审查项

### 6. Shell 命令注入风险

**文件**: [`pkgs/ship/app/components/user_manager.py`](pkgs/ship/app/components/user_manager.py:240)

**代码**:
```python
sudo_args.extend([
    "bash",
    "-lc",
    f"cd {shlex.quote(str(working_dir))} && {command}",  # command 未转义
])
```

**审查要点**:
- [ ] `command` 直接拼接到 shell 命令中，虽然前面有 `shlex.quote(working_dir)`
- [ ] 用户传入的 `command` 可以包含任意 shell 命令（这可能是设计意图，但需要确认）
- [ ] 是否需要对危险命令做黑名单？
- [ ] cwd 参数已做校验，但 env 参数呢？

---

### 7. ✅ 后台进程内存泄露（已修复）

**文件**: [`pkgs/ship/app/components/user_manager.py`](pkgs/ship/app/components/user_manager.py:90)

**问题**: 后台进程注册表永远增长：

```python
_background_processes: Dict[str, "BackgroundProcessEntry"] = {}
```

**已解决**:
- [x] 进程完成后，条目会在 `get_background_processes()` 调用时自动清理
- [x] 新增 `_cleanup_completed_processes()` 内部函数
- [x] 清理策略：删除所有 `returncode is not None` 的条目

**修复代码** (2026-02-05):
```python
def _cleanup_completed_processes() -> int:
    """清理已完成的后台进程条目，返回清理数量。"""
    completed_ids = [
        process_id
        for process_id, entry in _background_processes.items()
        if entry.process.returncode is not None
    ]
    for process_id in completed_ids:
        del _background_processes[process_id]
    return len(completed_ids)


def get_background_processes() -> List[Dict]:
    """获取所有后台进程（自动清理已完成条目）"""
    _cleanup_completed_processes()
    # ... 返回进程列表
```

---

### 8. ⏸️ 配置热加载与缓存（暂不处理）

**文件**: [`pkgs/bay/app/config.py`](pkgs/bay/app/config.py:221)

**问题**: `@lru_cache` 导致配置无法动态更新：

```python
@lru_cache
def get_settings() -> Settings:
    ...
```

**审查要点**:
- [ ] 配置被永久缓存，无法热加载
- [ ] 测试中需要手动清除缓存：`get_settings.cache_clear()`
- [ ] 是否需要支持配置热更新？
- [ ] 考虑使用依赖注入而非全局单例

**2026-02-05 分析结论：暂不处理**

这是**设计意图**，不是问题：
- ✅ 配置应在启动时加载，运行时保持不变
- ✅ 需要更改配置时重启服务即可
- ✅ 测试场景可用 `get_settings.cache_clear()` 清除
- ✅ 除非有明确的热加载需求（目前没有）

---

### 9. ⏸️ 数据库事务边界（暂不处理）

**文件**: [`pkgs/bay/app/managers/sandbox/sandbox.py`](pkgs/bay/app/managers/sandbox/sandbox.py:381)

**问题**: `delete()` 方法中多次 commit，事务边界不清晰：

```python
async def delete(self, sandbox: Sandbox) -> None:
    # ... 销毁 sessions
    for session in sessions:
        await self._session_mgr.destroy(session)  # 可能有自己的 commit

    # 软删除 sandbox
    sandbox.deleted_at = datetime.utcnow()
    await self._db.commit()  # commit 1

    # 级联删除 managed cargo
    if cargo and cargo.managed:
        await self._cargo_mgr.delete(...)  # commit 2
```

**审查要点**:
- [ ] 如果中途失败，会留下部分删除的状态
- [ ] 应该使用单一事务包裹整个删除操作
- [ ] 或者改用最终一致性 + 重试机制

**2026-02-05 分析结论：暂不处理**

当前设计是合理的：
- `session.destroy()` 涉及容器销毁（外部操作），不应包含在数据库事务中
- `cargo.delete()` 涉及文件系统删除（外部操作），也不应包含在事务中
- 最终一致性 + GC 兜底是合理的架构选择

兜底机制已存在：
- ✅ 使用了锁保护，降低并发问题
- ✅ `OrphanContainerGC` 会最终清理孤儿容器
- ✅ `OrphanCargoGC` 会最终清理孤儿 cargo
- ✅ 软删除模式允许后续补偿

---

### 10. Profile 查找效率

**文件**: [`pkgs/bay/app/config.py`](pkgs/bay/app/config.py:186)

**问题**: 线性查找 Profile：

```python
def get_profile(self, profile_id: str) -> ProfileConfig | None:
    for profile in self.profiles:
        if profile.id == profile_id:
            return profile
    return None
```

**审查要点**:
- [ ] 每次请求都遍历整个 profiles 列表
- [ ] Profile 数量增多后性能下降
- [ ] 应该在初始化时构建 `dict[str, ProfileConfig]`

---

## 🟡 低优先级审查项

### 11. ✅ 错误类型命名冲突（已修复）

**文件**: [`pkgs/bay/app/errors.py`](pkgs/bay/app/errors.py:94)

**问题**: 自定义 `FileNotFoundError` 与 Python 内置类型同名

**已解决** (2026-02-05):
- [x] `FileNotFoundError` → `CargoFileNotFoundError`
- [x] `TimeoutError` → `RequestTimeoutError`
- [x] 更新 [`pkgs/bay/app/adapters/ship.py`](pkgs/bay/app/adapters/ship.py:20) 中的导入和使用

---

### 12. 日志信息完整性

**文件**: [`pkgs/bay/app/drivers/docker/docker.py`](pkgs/bay/app/drivers/docker/docker.py:149)

**问题**: TODO 注释表明 owner 信息缺失：

```python
container_labels = {
    "bay.owner": "default",  # TODO: get from session/sandbox
    ...
}
```

**审查要点**:
- [ ] 容器标签中的 owner 被硬编码为 "default"
- [ ] 影响多租户隔离和资源追踪
- [ ] 需要从 session/sandbox 传递真实 owner

---

### 13. ⏸️ 硬编码的端口和超时（暂不处理）

**文件**: 多处

**审查要点**:
- [ ] [`pkgs/bay/app/config.py:105`](pkgs/bay/app/config.py:105): `runtime_port: int | None = 8123`
- [ ] [`pkgs/bay/app/managers/session/session.py:210`](pkgs/bay/app/managers/session/session.py:210): `max_wait_seconds: float = 120.0`
- [ ] [`pkgs/bay/app/adapters/ship.py:55`](pkgs/bay/app/adapters/ship.py:55): `timeout: float = 30.0`
- [ ] 这些魔法数字应该集中到配置文件

**2026-02-05 分析结论：暂不处理**

当前设计是合理的：
- `runtime_port` 已在 `ProfileConfig` 中配置化
- `max_wait_seconds` 和 `timeout` 作为函数参数默认值，可被调用者覆盖
- 集中配置化需要改动配置结构，成本较高
- 当前默认值经过实践验证，足够使用

---

### 14. ✅ 类型注解不一致（已修复）

**文件**: [`pkgs/bay/app/api/v1/sandboxes.py`](pkgs/bay/app/api/v1/sandboxes.py:58)

**问题**: 函数参数类型注解缺失

**已解决** (2026-02-05):
- [x] `_sandbox_to_response()` 添加完整类型注解
- [x] 统一使用 `str | None` 风格（PEP 604）

```python
def _sandbox_to_response(
    sandbox: Sandbox, current_session: Session | None = None
) -> SandboxResponse:
```

---

### 15. 测试覆盖度

**文件**: `pkgs/bay/tests/`, `pkgs/ship/tests/`

**审查要点**:
- [ ] 单元测试是否覆盖了并发场景？
- [ ] 是否有针对资源泄露的测试？
- [ ] 是否有边界条件测试（如 TTL=0, 空命令等）？
- [ ] 集成测试是否模拟了网络分区、容器崩溃等异常场景？

---

## 📋 架构层面审查

### 16. ✅ GC 机制（已实现）

**状态**: 已完整实现后台 GC 调度器和各类回收任务

**文件**: [`pkgs/bay/app/services/gc/`](pkgs/bay/app/services/gc/)

```
pkgs/bay/app/services/gc/
├── scheduler.py          # GCScheduler - 后台调度器
├── coordinator.py        # GC 协调器
├── lifecycle.py          # GC 生命周期管理
├── base.py               # GCTask 基类
└── tasks/
    ├── idle_session.py       # IdleSessionGC
    ├── expired_sandbox.py    # ExpiredSandboxGC
    ├── orphan_cargo.py       # OrphanCargoGC
    └── orphan_container.py   # OrphanContainerGC
```

**已解决**:
- [x] **IdleSessionGC**：空闲 Session 回收（idle_expires_at 过期）
- [x] **ExpiredSandboxGC**：过期 Sandbox 清理（expires_at 过期）
- [x] **OrphanCargoGC**：孤儿 managed cargo 清理
- [x] **OrphanContainerGC**：孤儿容器检测与清理
- [x] GC 调度器框架（GCTask + GCScheduler）
- [x] 配置化 GC 间隔与开关
- [x] Admin API 支持手动触发 GC（用于测试）
- [x] 单元测试 (`test_gc_scheduler.py`, `test_gc_tasks.py`)
- [x] E2E 测试 (`test_gc_e2e.py`, `test_gc_workflow_scenario.py`)

**待完成**:
- [ ] 启动时 reconcile（对账孤儿资源）

---

### 17. 可观测性缺失

**审查要点**:
- [ ] 无 Prometheus metrics 埋点
- [ ] 无分布式追踪 (OpenTelemetry)
- [ ] 日志缺少请求上下文（虽然有 request_id，但未贯穿）
- [ ] 无健康检查的详细状态（仅返回 `{"status": "healthy"}`）

**轻量化建议**:
- Metrics: 考虑 `prometheus-fastapi-instrumentator`（<100行接入）
- Tracing: 暂缓，等有真实排查需求再加
- 健康检查: 增加数据库连接检测、Docker 连接检测即可

---

### 18. 数据迁移策略

**审查要点**:
- [ ] 使用 SQLite 作为默认数据库，生产环境迁移路径不清晰
- [ ] 无 Alembic 迁移脚本（或未找到）
- [ ] Model 变更后的向后兼容性

**轻量化建议**:
- 保持 SQLite 作为默认选项，对于绝大多数单机部署场景足够
- 仅当明确需要多实例时再考虑 PostgreSQL
- 用 `alembic` 管理迁移，它是标准做法且足够轻量

---

## 🔧 语言选型讨论：Python vs Rust/Go

### 现状分析

当前项目全栈使用 Python (FastAPI)，这是一个**合理的初期选择**：
- 开发速度快，生态丰富
- 团队熟悉度高（假设）
- 原型验证阶段足够

### 考虑引入 Rust/Go 的场景

| 组件 | 当前语言 | 是否值得重写 | 理由 |
|:---|:---|:---|:---|
| **Bay (编排层)** | Python | ⚠️ 可考虑 Go | 高并发 API 网关，Go 的 goroutine 模型更轻量 |
| **Ship (运行时)** | Python | ❌ 不建议 | 核心是 IPython 内核，必须用 Python |
| **Driver 层** | Python | 🟡 远期考虑 | 与 Docker/K8s API 交互，Go 有天然优势 |
| **路径校验/安全** | Python | ✅ 可考虑 Rust | 性能敏感 + 安全关键路径 |

### 🟢 推荐策略：渐进式混合架构

**Phase 1：保持现状**
- 当前 Python 代码能正常工作
- 重写的成本远大于收益
- 先把功能做完，再考虑优化

**Phase 2：识别热点路径** (如果遇到性能瓶颈)
- 用 profiler 找出真正的瓶颈
- 通常是 10% 的代码占 90% 的时间

**Phase 3：局部重写** (如果有真实需求)

可优先考虑用 Rust/Go 重写的部分：

1. **Bay 核心**（如果选择重写）
   ```
   Go 优势：
   - 编译为单一二进制，部署简单
   - goroutine 比 asyncio 更轻量
   - 内存占用远低于 Python
   - 启动时间 <100ms
   ```

2. **路径安全校验 FFI 模块**
   ```
   Rust 优势：
   - 内存安全，无 GC
   - 可编译为 Python 扩展 (PyO3)
   - 适合安全关键逻辑
   ```

### ❌ 不建议重写的部分

1. **Ship 运行时**
   - 核心是 IPython 内核（纯 Python）
   - 与 Python 生态深度绑定
   - 用其他语言反而增加复杂度

2. **SDK**
   - 目标用户是 Python/AI 开发者
   - 保持 Python 是正确选择

### 💡 Linus 的观点

```
"我是个该死的实用主义者。"

用什么语言不重要，重要的是：
1. 这是个真问题还是臆想的？—— 你现在有性能问题吗？
2. 解决方案的复杂度是否与问题匹配？—— 重写的成本 vs 收益
3. 团队能否维护？—— 引入新语言意味着新的学习曲线

如果 Python 性能真的成为瓶颈（而不是"理论上可能"），
再考虑用 Go 重写 Bay 编排层。
但 Ship 必须保持 Python——这是它的核心价值。
```

### 结论

| 决策点 | 建议 |
|:---|:---|
| 现阶段 | 保持全 Python，专注功能完成 |
| 遇到 CPU 瓶颈 | 用 Go 重写 Bay |
| 遇到内存瓶颈 | 优化现有 Python 代码 + 考虑 Rust FFI |
| Ship 运行时 | 永远保持 Python |

---

## ✅ 已做得较好的部分

1. **数据模型设计**: Sandbox/Session/Cargo 分离清晰，`desired_state` vs `observed_state` 是正确的模式
2. **幂等性支持**: Idempotency-Key 机制完整
3. **路径隔离**: Ship 侧的 `resolve_path()` 实现正确，**Bay 侧也已实现双层防护**
4. **Profile 抽象**: 运行时规格枚举化，避免无限自定义
5. **Adapter 模式**: ShipAdapter 为未来多运行时扩展留出了接口
6. **轻量化架构**: 仅依赖 FastAPI + SQLite + Docker，无 Redis/etcd/消息队列，部署简单
7. **GC 机制**: 完整实现后台调度器 + 四种 GC 任务（Idle Session / Expired Sandbox / Orphan Cargo / Orphan Container）
8. **Extend TTL**: 支持延长 Sandbox TTL，带幂等性保护
9. **并发锁模块**: 独立的锁管理模块，支持锁清理
10. **测试覆盖**: 并行测试支持（pytest-xdist），E2E 场景丰富

---

## 💡 轻量化原则提醒

在解决上述问题时，务必遵循：

1. **"解决实际问题，不解决想象的问题"**
   - 单实例部署占 90% 场景，优先保证它能用
   - 多实例支持可以通过数据库乐观锁解决，无需 Redis

2. **"最简单的代码是最好的代码"**
   - 清理机制用简单的条件判断，不引入后台任务框架
   - 连接池复用是零成本优化，应该立刻做

3. **"不要过度抽象"**
   - 现有的 Driver/Adapter 抽象已经足够
   - 不需要为"未来可能"的场景添加更多层

---

## 审查优先级总结

| 优先级 | 项目数 | 关键问题 |
|:---:|:---:|:---|
| 🔴 高 | 3 | ~~并发锁~~ ✅、~~路径安全~~ ✅、资源泄露、时间竞态、连接管理 |
| 🟠 中 | 5 | 命令注入、内存泄露、配置缓存、事务边界、查找效率 |
| 🟡 低 | 5 | 命名冲突、日志完整性、硬编码、类型注解、测试覆盖 |
| 📋 架构 | 2 | ~~GC机制~~ ✅、可观测性、数据迁移 |

### 进度统计

- **已解决**: 8 项（并发锁改进、路径安全、GC 机制、httpx 连接管理、Session 启动失败清理、后台进程内存泄露、错误类型命名冲突、类型注解不一致）
- **暂不处理**: 4 项（时间竞态条件、配置热加载、数据库事务边界、硬编码端口超时）
- **待评估**: 6 项

---

> **下一步**: 按优先级逐项解决，每项完成后在此文档标记 ✅
>
> **注意**: 所有修复方案应优先选择不引入新依赖的实现方式

---

## 📝 变更历史

| 日期 | 变更内容 |
|:---|:---|
| 2026-02-05 | **#11 错误类型命名冲突已修复**、**#14 类型注解已修复**、#13 硬编码暂不处理；修复 config.yaml workspace→cargo 命名 |
| 2026-02-05 | 分析 #4/#7/#8/#9：时间竞态条件暂不处理、**后台进程内存泄露已修复**、配置热加载暂不处理、事务边界暂不处理 |
| 2026-02-02 | 更新 GC 机制为已完成；更新路径安全为已完成；更新并发锁为已改进；新增已做得好的部分 |
| 2026-01-31 | 初始版本 |
