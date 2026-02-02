# Phase 1.5 GC 实现计划（问题驱动版，结合现有代码）

> 目标：把 [`TODO.md`](TODO.md:1)、[`plans/phase-1/gc-design.md`](plans/phase-1/gc-design.md:1)、[`plans/phase-1/phase-1.md`](plans/phase-1/phase-1.md:1) 的 GC 设想，结合当前 Bay 的真实实现，落成一份 Phase 1.5 可执行的实现计划。
>
> Phase 1.5 范围（已确认）：**IdleSessionGC + ExpiredSandboxGC + OrphanCargoGC + OrphanContainerGC**，并且 **启动时 run_once** + **后台定时调度** 都要支持。
>
> 重要约束（你已强调）：Bay 常部署在“很多容器混跑”的自托管宿主机上；GC 必须 **宁可漏删也不可误删**。
>
> 重要演进约束：Phase 2 会引入 K8s runtime，因此 Phase 1.5 设计必须维持清晰抽象边界，避免把 GC 写死成 Docker 专用逻辑。

---

## 1. 我们要解决的具体问题

### 1.1 算力泄露

1) **空闲 sandbox 持有容器**：用户不再访问后，容器仍然运行（应按 `idle_timeout` 回收）。
- 现有机制：只有在 `stop` 或 `delete` 时会回收；没有后台扫描。
- 代码事实：`idle_expires_at` 会在 [`python.SandboxManager.ensure_running()`](pkgs/bay/app/managers/sandbox/sandbox.py:211) 更新，但没有任何地方消费这个字段进行回收。

2) **孤儿运行时实例**：异常/重启/竞态/人工操作导致：运行时实例仍存在（Docker container / K8s Pod），但 DB 中对应 [`python.Session`](pkgs/bay/app/models/session.py:30) 记录已不存在。
- Phase 1.5 重点解决 Docker 容器。
- Phase 2 扩展到 K8s Pod。

### 1.2 存储泄露

3) **过期 sandbox 的残留资源**：sandbox TTL 到期后，如果没有显式 delete，会一直占用 volume/DB 记录。
- 代码事实：[`python.Sandbox.is_expired`](pkgs/bay/app/models/sandbox.py:76) 只提供状态计算；没有后台清理。

4) **孤儿 managed cargo**：managed cargo 绑定 sandbox 生命周期，但级联删除失败/部分失败会残留 volume 或 DB 记录。
- 代码事实：[`python.CargoManager.delete()`](pkgs/bay/app/managers/cargo/cargo.py:158) 先删 volume 再删 DB；若 volume 删除失败会抛异常并保留 DB；若 DB 删除失败可能导致 volume 已删但 DB 残留（或反过来）。

---

## 2. 现有系统与我们能复用的能力（避免重新发明）

### 2.1 现有 Manager 语义应复用

- Sandbox 删除语义（destroy sessions + soft delete sandbox + 级联删 managed cargo）已经封装在 [`python.SandboxManager.delete()`](pkgs/bay/app/managers/sandbox/sandbox.py:381)。
- Session stop/destroy 已分离：
  - [`python.SessionManager.stop()`](pkgs/bay/app/managers/session/session.py:241) 释放算力但保留 session 记录
  - [`python.SessionManager.destroy()`](pkgs/bay/app/managers/session/session.py:261) 删除运行时实例并硬删 session 记录
- 并发保护：[`python.SandboxManager.ensure_running()`](pkgs/bay/app/managers/sandbox/sandbox.py:211) 使用 sandbox 级别 in-memory lock（并尝试 `FOR UPDATE`）。

### 2.2 Driver 已经是抽象层，GC 必须继续走 Driver

- 当前容器 lifecycle 都经过 [`python.Driver`](pkgs/bay/app/drivers/base.py:51)，具体实现在 [`python.DockerDriver`](pkgs/bay/app/drivers/docker/docker.py:50)。
- Phase 2 引入 K8sDriver 后，GC 逻辑应尽量不改，只替换 driver 实现。

---

## 3. Phase 1.5 需要新增/修改的最小代码面

### 3.1 新增 GC 框架（内部服务，不对外暴露 API）

建议新增目录：

- [`pkgs/bay/app/services/gc/`](pkgs/bay/app/services/gc/:1)
  - [`pkgs/bay/app/services/gc/base.py`](pkgs/bay/app/services/gc/base.py:1)：任务接口与结果结构（例如 [`python.GCTask`](pkgs/bay/app/services/gc/base.py:1)、[`python.GCResult`](pkgs/bay/app/services/gc/base.py:1)）
  - [`pkgs/bay/app/services/gc/scheduler.py`](pkgs/bay/app/services/gc/scheduler.py:1)：调度器（例如 [`python.GCScheduler`](pkgs/bay/app/services/gc/scheduler.py:1)）
  - [`pkgs/bay/app/services/gc/tasks/`](pkgs/bay/app/services/gc/tasks/:1)：四个任务实现

### 3.2 FastAPI 生命周期接入（已确认）

在 [`python.lifespan()`](pkgs/bay/app/main.py:20) 中接入：

- startup：`init_db()` 后
  1) 构建 driver + db session factory
     - driver：复用全局单例（避免重复创建 docker client），直接调用 [`python.get_driver()`](pkgs/bay/app/api/dependencies.py:30)
     - db session factory：GC 每轮/每 task 使用独立的 [`python.AsyncSession`](pkgs/bay/app/db/session.py:8)
       - 建议方式 1：直接复用 [`python.get_async_session()`](pkgs/bay/app/db/session.py:68) 作为 context manager（与现有事务处理逻辑一致）
       - 建议方式 2：在 [`pkgs/bay/app/db/session.py`](pkgs/bay/app/db/session.py:1) 暴露一个公共 `get_session_factory()` 包装内部的 [`python._get_session_factory()`](pkgs/bay/app/db/session.py:36)，供后台任务使用（避免从外部 import underscore 函数）
  2) 构建 GC tasks + scheduler
  3) `run_once()`（受 `gc.run_on_startup` 控制）
  4) `start()` 启动后台循环（受 `gc.enabled` 控制）

- shutdown：
  - `stop()` 停止后台任务
  - `close_db()`

**重入保护（已确认）**

- `run_once()` 与后台 loop 复用同一个内部执行函数（例如 scheduler 内部的 `_run_one_cycle()`）。
- 执行时通过 scheduler 的 `coordinator`（Phase 1.5 为 `NoopCoordinator`）+ in-memory mutex 防止并发重入。

**与现有源码对齐的注意点（补充）**

- 不要在 GC 框架里自行 new 一个 driver：API 层已经通过 [`python.get_driver()`](pkgs/bay/app/api/dependencies.py:30) 做了 singleton 缓存（lru_cache）。GC 复用同一个 driver 可以避免重复连接与资源泄露。
- GC 运行期不要复用 FastAPI 请求链路里的 db session：GC 是后台任务，应始终自己开/关独立的 db session（参考 [`python.get_async_session()`](pkgs/bay/app/db/session.py:68) 的用法）。

### 3.3 Driver 扩展（为 OrphanContainerGC 补齐“列出运行时实例”的抽象能力；已确认）

#### 3.3.1 为什么必须抽象

OrphanContainerGC 本质是“列出所有 Bay 管理的运行时实例 → 与 DB session 对账 → 清理孤儿”。

- 在 Docker 下：运行时实例是 container
- 在 K8s 下：运行时实例是 Pod（Phase 2）

当前 [`python.Driver`](pkgs/bay/app/drivers/base.py:51) 只支持在已知 `container_id` 时进行 start/stop/destroy/status/logs；缺少“枚举所有实例”的能力，因此无法发现 `Session` 已不存在但运行时实例仍残留的孤儿。

#### 3.3.2 已确认的接口形态（Phase 1.5 实现 Docker，Phase 2 实现 K8s）

- 保留现有 container 生命周期 API（create/start/stop/destroy/status/logs）不动，避免 Phase 1.5 大范围重命名。
- 仅为 GC 增量补齐两个新接口（GC 只调用这两个接口，不直接调用 container destroy）：

在 [`python.Driver`](pkgs/bay/app/drivers/base.py:51) 增加：

- `list_runtime_instances(*, labels: dict[str, str]) -> list[RuntimeInstance]`
  - 语义：只返回满足 label 过滤的实例（强过滤是防误删第一道闸）
  - `RuntimeInstance` 最小字段：`id`、`name`、`labels`
- `destroy_runtime_instance(instance_id: str) -> None`
  - 语义：强制删除该实例（docker 下对应 container delete(force=True)，k8s 下对应 pod delete）

备注：
- Phase 1.5 的 Docker 实现映射到 docker container list/delete。
- Phase 2 的 K8s 实现映射到 list Pods / delete Pod。

> 这两个方法是 OrphanContainerGC 的唯一依赖点，用于维持跨 runtime 的抽象层与 strict 安全策略。

---

## 4. 四个 GC 任务的行为定义（需要敲定的具体行为）

### 4.1 IdleSessionGC（回收空闲 session/运行时实例）

**触发条件（DB 维度）**
- `sandbox.deleted_at is null`
- `sandbox.idle_expires_at is not null and < now`

字段来源：[`python.Sandbox.idle_expires_at`](pkgs/bay/app/models/sandbox.py:55)

**核心行为（已拍板）**

- IdleSessionGC 对命中的 sandbox：
  1) 找到其全部 sessions（`sessions.sandbox_id == sandbox.id`）
  2) 对每个 session 调 [`python.SessionManager.destroy()`](pkgs/bay/app/managers/session/session.py:261)
  3) 更新 sandbox：`current_session_id = null` + `idle_expires_at = null`

理由：
- Idle 回收目标是释放算力；Session 不对外暴露，保留大量 STOPPED sessions 的收益很低。
- 与 [`plans/phase-1/gc-design.md`](plans/phase-1/gc-design.md:724) 保持一致：session 记录硬删除，孤儿运行时实例由 OrphanContainerGC 兜底。

**竞态策略（必须与 ensure_running / stop / delete 共存；已确认方案 A + 细节敲定）**

- 处理单个 sandbox 时应使用同一把 sandbox 级 in-memory lock。

**已确认落地细节（对齐现有实现）**

1) 统一锁来源：新建公共模块 [`pkgs/bay/app/concurrency/locks.py`](pkgs/bay/app/concurrency/locks.py:1)，将目前在 [`python._get_sandbox_lock()`](pkgs/bay/app/managers/sandbox/sandbox.py:43) 的实现迁移过去（并提供清理方法）。

2) 锁归属在 manager 内部（S1）：
- 除了 [`python.SandboxManager.ensure_running()`](pkgs/bay/app/managers/sandbox/sandbox.py:211)，还要在 [`python.SandboxManager.stop()`](pkgs/bay/app/managers/sandbox/sandbox.py:357) 与 [`python.SandboxManager.delete()`](pkgs/bay/app/managers/sandbox/sandbox.py:381) 内部获取同一把 `get_sandbox_lock(sandbox_id)`。
- 这样 API/GC/未来 CLI 只要调用 manager，就不会漏锁。

3) 锁内 DB 一致性：
- 参考 ensure_running 的做法，stop/delete 在进入 lock 后也要 `await self._db.rollback()` 并重新 `select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()` 获取最新 sandbox（避免 SQLite 快照/过期对象问题）。
- stop/delete 的参数建议由“传入 Sandbox 对象”逐步迁移为“传入 sandbox_id + owner”，或在方法内部使用传入对象提取 id 后立即 refetch 并只操作 refetch 的 `locked_sandbox`。

> 说明：该锁仅解决单进程/单实例内竞态；跨实例竞态在 Phase 2 由 scheduler coordinator（租约/lock）解决。

**已确认落地方案（A）：抽出公共锁模块，GC 与 SandboxManager 共用**

- 新增公共模块（建议路径）：[`pkgs/bay/app/concurrency/locks.py`](pkgs/bay/app/concurrency/locks.py:1)
- 将当前定义在 [`python._get_sandbox_lock()`](pkgs/bay/app/managers/sandbox/sandbox.py:43) 的逻辑迁移到公共模块（例如提供 [`python.get_sandbox_lock()`](pkgs/bay/app/concurrency/locks.py:1)）。
- [`python.SandboxManager.ensure_running()`](pkgs/bay/app/managers/sandbox/sandbox.py:211) 改为从公共模块取 lock。
- IdleSessionGC 同样从公共模块取 lock，并用 `async with lock:` 包裹“枚举 sessions → destroy → 更新 sandbox 字段”这一段。

**选择原因（已记录）**
- 解耦：GC 不需要依赖 SandboxManager 的内部 API 才能共享同一把锁。
- 分层清晰：并发锁属于进程内基础设施，放在 concurrency 层更合理。
- 便于未来扩展：后续新增其它后台任务也能复用同一套 sandbox 级互斥。

> 说明：该锁仅解决单进程/单实例内竞态；跨实例竞态在 Phase 2 由 scheduler coordinator（租约/lock）解决。

**失败策略**
- 单条 session destroy 失败：记录 error，继续处理后续 session；最终任务返回 cleaned/errors。

### 4.2 ExpiredSandboxGC（清理过期 sandbox）

**触发条件（DB 维度）**
- `sandbox.deleted_at is null`
- `sandbox.expires_at is not null and < now`

字段来源：[`python.Sandbox.expires_at`](pkgs/bay/app/models/sandbox.py:54)

**核心行为：过期后做什么？（已确认：走 delete 流程）**
- 对命中的 sandbox：调用 [`python.SandboxManager.delete()`](pkgs/bay/app/managers/sandbox/sandbox.py:381)
- 为避免与 [`python.SandboxManager.ensure_running()`](pkgs/bay/app/managers/sandbox/sandbox.py:211) 并发，建议在 GC 侧或 manager 内部确保 `delete()` 也使用同一把 sandbox lock（见上文 IdleSessionGC 竞态策略补充）。

理由：
- delete 已封装“destroy sessions + 软删除 sandbox + managed cargo 级联删除”。
- Phase 1.5 的目标是尽快回收资源（算力 + cargo/volume），并保持改动面最小。

**对外语义（已确认）：`EXPIRED → DELETED` 是允许且预期的状态跃迁**
- 在 `expires_at` 过期到 ExpiredSandboxGC 下一次扫描运行之间，sandbox 可能短暂呈现 `EXPIRED`（由 [`python.Sandbox.compute_status()`](pkgs/bay/app/models/sandbox.py:83) 的 `expires_at < now` 判定产生）。
- 一旦 ExpiredSandboxGC 运行并调用 delete 写入 `deleted_at`，由于 `compute_status` 优先判断 `deleted_at`，对外将呈现 `DELETED`。
- 该行为不引入额外 grace 机制；客户端应将 `DELETED` 视为终态（或至少是“已回收/不可再用”终态）。

### 4.3 OrphanCargoGC（清理孤儿 managed cargo）

**触发条件（DB 维度）**
- `cargo.managed == true`
- 且满足任一：
  - `cargo.managed_by_sandbox_id is null`
  - 或 join sandbox 后 `sandbox.deleted_at is not null`

字段来源：[`python.Cargo.managed`](pkgs/bay/app/models/cargo.py:32)、[`python.Cargo.managed_by_sandbox_id`](pkgs/bay/app/models/cargo.py:33)

**核心行为（建议：复用 CargoManager 的 internal delete；已确认）**

- 对命中的 cargo：通过 CargoManager 的 internal API 执行删除：
  1) `driver.delete_volume(cargo.driver_ref)`
  2) `db.delete(cargo)`

**已确认实现方式：在 CargoManager 增加 internal delete**

- 在 [`python.CargoManager`](pkgs/bay/app/managers/cargo/cargo.py:23) 增加 internal 方法（示例命名）：
  - `delete_internal_by_model(cargo: Cargo) -> None`
  - 语义：不做 owner 校验、不做 managed 冲突校验；仅执行“volume 删除 + DB 删除”。
- OrphanCargoGC 只负责“找出 orphan 集合”，然后对每个 cargo 调 internal delete。

**选择原因（已记录）**
- 删除语义集中：volume/DB 删除顺序、not_found 容忍、日志字段统一在 manager。
- 演进友好：未来 backend 变成 `k8s_pvc` 等，只需要扩展 manager；GC 不用跟着改。
- 风险可控：internal API 不对外暴露，仅供 sandbox cascade delete / GC 使用。

### 4.4 OrphanContainerGC（清理孤儿运行时实例，严格防误删）

#### 4.4.1 安全原则（必须贯彻）

- **默认策略：strict 模式**
  - 只有当实例满足“强识别条件”时，才会进入 orphan 判定与删除。
  - 强识别条件设计目标：实例几乎不可能属于用户其他工作负载。
- **宁可漏删，不可误删**
  - 对无法确信归属的实例：只记录日志（例如 `gc.orphan_container.skip_untrusted`），不做删除。

#### 4.4.2 强识别条件（已拍板，AND 关系）

strict 模式只考虑同时满足以下条件的实例：

1) `name` 以 `bay-session-` 开头
   - Docker 创建点见 [`python.DockerDriver.create()`](pkgs/bay/app/drivers/docker/docker.py:231)
   - Phase 2 的 K8sDriver 也应复用该 naming 约定（Pod 名或生成前缀）
2) labels 同时包含：
   - `bay.session_id`
   - `bay.sandbox_id`
   - `bay.workspace_id`
   - `bay.instance_id`
   - `bay.managed`
3) `labels["bay.instance_id"]` 必须等于配置的 `gc.instance_id`
4) `labels["bay.managed"] == "true"`

> 说明：当前容器 labels 构建在 [`python.DockerDriver.create()`](pkgs/bay/app/drivers/docker/docker.py:134) 内，Phase 1.5 需要补上 `bay.instance_id` 与 `bay.managed=true`。

#### 4.4.3 orphan 判定与删除动作

在强识别条件通过后：

- 读取 `labels["bay.session_id"]`，去 DB 查 [`python.Session`](pkgs/bay/app/models/session.py:30)
- `Session` 不存在 ⇒ 该实例为 orphan
- 删除：调用 `driver.destroy_runtime_instance(instance_id)`

---

## 5. 调度策略（如何跑、多久跑、遇到错误怎么跑）

### 5.1 运行时形态（建议：单循环串行执行）

Phase 1.5 推荐：scheduler 用单个后台循环，按固定顺序依次执行四个任务。
- 好处：减少竞态、减少对 runtime/DB 的并发压力、日志更容易理解。

### 5.2 任务执行顺序（建议）

1. IdleSessionGC（先释放算力）
2. ExpiredSandboxGC（再按 TTL 清理整套资源）
3. OrphanCargoGC（兜底清理 managed cargo 残留）
4. OrphanContainerGC（最后兜底清理运行时残留；严格防误删）

### 5.3 错误处理

- task 级：捕获异常，返回 errors，并继续下一轮（不能因为一次异常导致 GC 全停）。
- item 级：逐条处理，单条失败不影响其他条。

### 5.4 部署假设与多实例限制（Phase 1.5 已确认）

- Phase 1.5 **按单实例为主**实现：不做跨实例的 leader/租约/全局锁。
- 如果用户以多实例同库方式部署：可能出现多实例并行执行 GC 的情况。
- 我们的防护策略是：
  - **任务幂等**：查询条件带 `deleted_at is null` 等硬门槛；重复执行不会导致错误状态扩散。
  - **严格防误删**：OrphanContainerGC 只处理满足强识别条件的实例（尤其是 `bay.instance_id == gc.instance_id` + `bay.managed == true`），避免跨实例误删。
- 因此 Phase 1.5 的安全目标是：**宁可漏删，不可误删**；多实例下更倾向于漏删。

### 5.5 Phase 2 全局互斥的结构预留（不在 Phase 1.5 落地）

为避免未来改动面扩散，Phase 1.5 在 GC 框架中预留“可插拔协调器”概念：

- 在 scheduler 外围引入 `coordinator`（例如 [`python.GCRunCoordinator.acquire()`](pkgs/bay/app/services/gc/coordinator.py:1) / release 形态），用于控制本轮 GC 是否允许执行。
- Phase 1.5 默认实现为 `NoopCoordinator`：总是允许执行。
- Phase 2 再替换为 DB 租约/lock 实现（例如 `DbLeaseCoordinator`），以实现多实例 leader。

> 关键点：互斥能力只存在于 scheduler 外围，不渗透到各 task 内部逻辑。

---

## 6. 配置落点（Phase 1.5 最小配置；已确认默认策略）

> 已确认：Phase 1.5 **不引入 dry-run 开关**。安全性主要依赖 strict 识别门槛（尤其是 OrphanContainerGC）与完善日志。

### 6.1 默认策略（开箱即用优先）

- `gc.enabled` 默认 **true**：默认启动后台 GC。
- `gc.run_on_startup` 默认 **true**：启动后立即 `run_once()`。
- `gc.interval_seconds` 默认 **300**（5 分钟）：后台循环间隔。
- `gc.instance_id` 默认从环境派生：
  - 优先取 `BAY_GC__INSTANCE_ID`（显式配置，最高优先级，推荐）
  - 否则取 `HOSTNAME`（容器名/主机名）
  - 最后兜底为字符串 `bay`

> 说明：从 `HOSTNAME` 派生在“单实例/单容器”场景通常足够；但多实例同宿主机或同编排环境下可能发生冲突，从而削弱 `bay.instance_id == gc.instance_id` 这道安全闸。文档需明确：多实例部署时应显式配置 `gc.instance_id`。

### 6.2 配置字段（Phase 1.5）

在 [`pkgs/bay/app/config.py`](pkgs/bay/app/config.py:1) 增加 `gc` 配置（建议字段）：

- `gc.enabled: bool`
- `gc.run_on_startup: bool`
- `gc.interval_seconds: int`
- `gc.instance_id: str`（OrphanContainerGC strict 模式硬门槛；默认从环境派生，但推荐显式配置）
- `gc.tasks.idle_session.enabled: bool`
- `gc.tasks.expired_sandbox.enabled: bool`
- `gc.tasks.orphan_workspace.enabled: bool`
- `gc.tasks.orphan_container.enabled: bool`

并在 [`pkgs/bay/config.yaml.example`](pkgs/bay/config.yaml.example:1) 增加示例与注释说明。

---

## 7. 测试与验收（强调防误删）

### 7.1 单元测试（优先保证框架正确性）

- scheduler：
  - `run_once` 会按顺序调用任务
  - 后台 loop 能被 stop/cancel
- tasks（DB 查询条件）：
  - 用 sqlite async（项目默认）构造数据，验证命中集正确

### 7.2 集成测试（最少 3 条，覆盖兜底 + 防误删）

建议新增到 [`pkgs/bay/tests/integration/`](pkgs/bay/tests/integration/:1)：

1) IdleSessionGC E2E
- create sandbox → exec 触发 session/container
- 把 `idle_expires_at` 改到过去（直接 update DB）
- 触发 `gc.run_once()`
- 断言：session 记录被删除，且后续 exec 会拉起新的 session

2) OrphanContainerGC E2E（strict 模式）
- 制造一个“可信实例”：同时具备 name 前缀 + 4 个 labels（含 `bay.instance_id==gc.instance_id`），但 DB 中没有该 session
- `gc.run_once()`
- 断言：实例被删除

3) OrphanContainerGC E2E（防误删）
- 制造一个“不可信实例”：缺少 `bay.instance_id` 或 `bay.instance_id!=gc.instance_id` 或缺少核心 labels
- `gc.run_once()`
- 断言：不会被删除（只记录 skip 日志）

---

## 8. Phase 1.5 不做（防止扩 scope）

- Admin API：例如 `/admin/gc` 手动触发与状态查询（如需，可放 Phase 2 或 Phase 1.5+）
- 分布式部署下的全局去重/锁（Phase 1.5 以单实例为主；多实例需要 leader/锁/租约）
- Prometheus metrics（属于可观测性增强项，见 [`TODO.md`](TODO.md:130)）
