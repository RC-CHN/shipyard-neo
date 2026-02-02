# Shipyard Neo 项目待办清单

> 更新日期：2026-02-02
> 
> 本文档追踪项目级别的待办事项和演进路线。详细设计请参考 [`plans/`](plans/) 目录。

## 📊 总体进度概览

```
Phase 1 (MVP)      [████████████████████░░░░] 85%
Phase 1.5 (P1)     [██████░░░░░░░░░░░░░░░░░░] 25%
Phase 2            [██░░░░░░░░░░░░░░░░░░░░░░] 10%
```

---

## ✅ Phase 1 - 已完成

### Bay 核心 (100%)

- [x] FastAPI 项目骨架搭建
- [x] SQLite 数据库 + SQLModel ORM
- [x] Sandbox/Session/Cargo 模型定义
- [x] DockerDriver 实现（支持 host_port/container_network 模式）
- [x] SandboxManager 生命周期管理
- [x] SessionManager + ensure_running（含 runtime readiness 等待）
- [x] CargoManager（Docker Volume 后端）
- [x] CapabilityRouter（能力路由）
- [x] ShipAdapter（HTTP 客户端）

### Bay API (100%)

- [x] `POST /v1/sandboxes` - 创建 Sandbox
- [x] `GET /v1/sandboxes` - 列出 Sandboxes
- [x] `GET /v1/sandboxes/{id}` - 查询 Sandbox
- [x] `POST /v1/sandboxes/{id}/keepalive` - 保持活跃
- [x] `POST /v1/sandboxes/{id}/stop` - 回收算力
- [x] `DELETE /v1/sandboxes/{id}` - 彻底销毁
- [x] `POST /v1/sandboxes/{id}/python/exec` - Python 执行
- [x] `POST /v1/sandboxes/{id}/shell/exec` - Shell 执行
- [x] `GET/PUT/DELETE /v1/sandboxes/{id}/filesystem/*` - 文件操作
- [x] `POST /v1/sandboxes/{id}/filesystem/upload` - 文件上传
- [x] `GET /v1/sandboxes/{id}/filesystem/download` - 文件下载

### 鉴权与安全 (100%)

- [x] API Key 认证（`authenticate()` + `AuthDep`）
- [x] Owner 隔离
- [x] 可配置 `allow_anonymous` 开发模式
- [x] `X-Owner` header（开发测试用）

### 幂等与并发 (100%)

- [x] IdempotencyService 实现
- [x] `POST /v1/sandboxes` 支持 `Idempotency-Key`
- [x] 并发 ensure_running 竞态修复（asyncio.Lock + 双重检查）

### Profile 能力检查 (100%)

- [x] `require_capability()` 工厂函数
- [x] 前置能力拦截（Profile 声明为硬约束）
- [x] 单元测试 + E2E 测试覆盖

### Ship 运行时 (100%)

- [x] IPython 内核管理（单例模式）
- [x] Shell 命令执行
- [x] Filesystem 组件
- [x] Terminal PTY 支持
- [x] `GET /meta` 运行时自描述接口
- [x] Docker 镜像构建

### 测试 (100%)

- [x] 97 个单元测试（Bay）
- [x] 33 个 E2E 测试（Bay）
- [x] docker-host / docker-network 两种测试模式

---

## 🚨 P0 - 最高优先级：命名重构（Workspace → Cargo）

> **决策**：将 Workspace 重命名为 Cargo，延续航海拟物化命名风格
>
> **理由**：Cargo（货物）是船运输的核心对象，语义上比 Locker 更准确——存储的是"要处理的数据"而非"存放东西的地方"

**命名体系**：
```
🏖️ Bay    - 港湾 (管理层，调度中心)
🚢 Ship   - 船 (运行时，计算载体)
📦 Cargo  - 货物 (数据持久化，要处理的内容)
```

**重命名范围**：

- [x] **设计文档更新**
  - [x] `plans/bay-design.md` - 概念模型中 Workspace → Cargo
  - [x] `plans/bay-concepts.md` - 数据概念更新
  - [x] `plans/bay-api.md` - API 路径更新 `/workspaces` → `/cargos`
  - [x] `plans/phase-1/*.md` - 相关引用更新
- [x] **Bay 代码重构**
  - [x] `pkgs/bay/app/models/workspace.py` → `cargo.py`
  - [x] `pkgs/bay/app/managers/workspace/` → `cargo/`
  - [ ] Cargo API 路由 `/v1/cargos`（实现见“Cargo API”任务；当前无 `/v1/workspaces` 路由）
  - [x] 数据库表名 `workspaces` → `cargos`
  - [x] 字段名 `workspace_id` → `cargo_id`
  - [x] `managed_by_sandbox_id` 保持不变
- [x] **Ship（/workspace）说明**
  - [x] Ship 内部仍保留 `workspace` 命名（路径沙箱与固定挂载点），不与 Bay 的 Cargo 概念混用
  - [x] 挂载路径保持 `/workspace`（运行时契约）
- [x] **测试更新**
  - [x] 单元测试文件和用例更新
  - [x] E2E 测试更新
- [ ] **SDK 更新**
  - [ ] `sdk-reference/` 中的引用更新
- [x] **README 和文档更新**
  - [x] 根目录 README.md
  - [x] 各子包 README.md

---

## 🚧 Phase 1.5 (P1) - 进行中

### 路径安全校验

- [x] Bay 侧路径校验实现（禁止绝对路径、目录穿越）
- [x] 与 Ship `resolve_path` 对齐
- [x] 单元测试覆盖

### 可观测性增强

- [ ] Prometheus metrics 暴露
- [ ] 结构化日志完善
- [ ] 错误追踪增强

---

## 📋 Phase 2 - 待开发

### 🔴 高优先级：GC 机制

> 详见 [`plans/phase-1/gc-design.md`](plans/phase-1/gc-design.md)

- [x] **IdleSessionGC**：空闲 Session 回收（idle_expires_at 过期）
- [x] **ExpiredSandboxGC**：过期 Sandbox 清理（expires_at 过期）
- [x] **OrphanCargoGC**：孤儿 managed cargo 清理
- [x] **OrphanContainerGC**：孤儿容器检测与清理（默认禁用，需要 strict labels）
- [x] GC 调度器框架（GCTask + GCScheduler）
- [x] 启动时 reconcile
- [x] 配置化 GC 间隔与开关

### ✅ Extend TTL (已完成)

> 详见 [`plans/phase-1/gc-design.md#8.3`](plans/phase-1/gc-design.md)

- [x] `POST /v1/sandboxes/{id}/extend_ttl` API 实现
- [x] expires_at 计算规则（max(old, now) + extend_by）
- [x] 拒绝复活已过期 Sandbox (409 `sandbox_expired`)
- [x] 拒绝延长 TTL=null 的 Sandbox (409 `sandbox_ttl_infinite`)
- [x] Idempotency-Key 支持
- [x] E2E 测试覆盖 (`test_extend_ttl.py`, `test_long_running_extend_ttl.py`)

### 🟠 中优先级：Cargo API

> 详见 [`plans/bay-api.md#6.3`](plans/bay-api.md)（重命名后）

- [ ] `POST /v1/cargos` - 创建独立 Cargo
- [ ] `GET /v1/cargos` - 列出 Cargos
- [ ] `GET /v1/cargos/{id}` - 查询 Cargo
- [ ] `DELETE /v1/cargos/{id}` - 删除 Cargo
- [ ] `POST /v1/cargos/{id}/files/read` - 直读文件
- [ ] `POST /v1/cargos/{id}/files/write` - 直写文件
- [ ] 权限控制（更高 scope）
- [ ] managed vs external 删除规则

### 🟠 中优先级：SDK 完善

> 当前 SDK 为参考实现，需与新 Bay API 对齐

- [ ] 与 Bay `/v1/*` API 对齐
- [ ] 错误处理增强
- [ ] 类型定义完善
- [ ] 文档与示例更新
- [ ] 发布到 PyPI

### 🟡 中优先级：MCP 协议层

> 详见 [`plans/ship-refactor-and-mcp.md`](plans/ship-refactor-and-mcp.md)

- [ ] Ship: `user_manager.py` → `process_manager.py` 重命名
- [ ] Ship: MCP over SSE 传输层实现
- [ ] Ship: 现有能力注册为 MCP Tools
- [ ] Ship: Workspace 暴露为 MCP Resources
- [ ] Ship: `GET /capabilities` 能力清单接口
- [ ] Bay: MCP 连接与工具发现

### 🟡 低优先级：多容器支持

> 详见 [`plans/phase-2/phase-2.md`](plans/phase-2/phase-2.md)

- [ ] ProfileConfig 扩展（多容器定义）
- [ ] Session 模型扩展（containers 列表）
- [ ] DockerDriver 多容器创建与网络互通
- [ ] CapabilityRouter 智能路由（Primary 处理者）
- [ ] Browser 容器镜像（Playwright）
- [ ] BrowserAdapter 实现

### 🟡 低优先级：K8s Driver

- [ ] K8sDriver 实现
- [ ] Pod + PVC 管理
- [ ] NetworkPolicy 配置
- [ ] 生产级部署文档

---

## 🔧 Phase 3 - 轻量化重构（可选）

> **背景**：有 AI 辅助开发，技术复杂性不再是障碍。以下重构可显著降低资源占用。
>
> **详见**：[`REVIEW.md#语言选型讨论`](REVIEW.md#-语言选型讨论python-vs-rustgo)

### 🟢 Bay 编排层 Go 重写

**目标**：用 Go 重写 Bay，追求最小内存占用与最快启动速度

**收益**：
- 内存：Python ~150MB → Go ~10-30MB（视依赖与驱动实现而定）
- 启动：Python ~2s → Go ~20-80ms
- 部署：单一二进制，无解释器依赖

**技术方案（极简优先）**：

- [ ] **Bay-Go HTTP 服务（零框架）**
  - [ ] 选型：Go 标准库 HTTP（不引入 Web 框架）
  - [ ] 项目骨架：`pkgs/bay-go/`
  - [ ] 路由：最小化自研 mux（按 method+path）
  - [ ] 配置：flag + 环境变量（必要时再引入配置库）
  - [ ] 日志：Go 标准库 slog（或最小化结构化输出）

- [ ] **数据层迁移（sqlc 优先）**
  - [ ] 代码生成：sqlc（类型安全 + 零运行时开销，Go 生态最佳实践）
  - [ ] 支持 SQLite + PostgreSQL
  - [ ] 迁移：golang-migrate
  - [ ] Repository 层设计：所有 SQL 集中到 `internal/repo/` 目录
  - [ ] **测试矩阵**
    - [ ] 单元测试（SQLite in-memory）：覆盖 CRUD 与边界条件
    - [ ] 事务一致性测试：create sandbox + locker + session 原子性
    - [ ] 并发测试：ensure_running 只产生 1 个 session
    - [ ] 集成测试（PostgreSQL 容器）：验证迁移 + 查询语义一致
    - [ ] SQL 注入回归用例：恶意输入测试
    - [ ] 静态检查（CI 必跑）：`go test -race` + `gosec` + `staticcheck`

- [ ] **Driver 层（Docker Go SDK）**
  - [ ] 选型：`github.com/docker/docker/client`（不使用 CLI 子进程）
  - [ ] 客户端单例复用：一次初始化，全局复用，线程安全
  - [ ] 端口映射/容器网络逻辑复用
  - [ ] **优化项**
    - [ ] 减少 inspect 调用：status 查询使用 ContainerList 替代 ContainerInspect
    - [ ] 并行 GC 操作：批量 stop/remove 使用 goroutine 并发执行
    - [ ] 连接复用：避免每次操作创建新连接

- [ ] **Manager 层**
  - [ ] SandboxManager：sync.Mutex（按 sandbox_id 粒度）
  - [ ] SessionManager：context 超时控制
  - [ ] 幂等性：数据库 UNIQUE 约束（避免引入额外组件）

- [ ] **API 层**
  - [ ] REST API 完全兼容 Python 版本
  - [ ] OpenAPI spec 复用

- [ ] **测试与验证**
  - [ ] 使用现有 E2E 测试验证兼容性
  - [ ] 性能基准对比（启动时间、内存、QPS）

### 🟡 路径安全模块 Rust FFI（可选）

**目标**：用 Rust 实现安全关键的路径校验逻辑，编译为 Python 扩展

**适用场景**：如果保持 Python Bay，但需要增强安全性

- [ ] **Rust 核心模块**
  - [ ] `path_validator` crate
  - [ ] 路径规范化、穿越检测
  - [ ] 使用 PyO3 绑定

- [ ] **Python 集成**
  - [ ] `bay-security` Python 包
  - [ ] 替换现有 `resolve_path` 调用

### 📊 重写优先级评估

| 组件 | 语言 | 优先级 | 预估工作量 | ROI |
|:---|:---|:---|:---|:---|
| Bay 编排层 | Go | ⭐⭐⭐ | 2-3 周 | 高：内存/启动/部署 |
| 路径安全 FFI | Rust | ⭐⭐ | 3-5 天 | 中：安全性增强 |
| Ship 运行时 | Python | 不重写 | - | N/A：核心依赖 IPython |
| SDK | Python | 不重写 | - | N/A：目标用户是 Python |

---

## 📁 相关文档索引

| 文档 | 说明 |
| :--- | :--- |
| [`plans/bay-design.md`](plans/bay-design.md) | Bay 架构设计 |
| [`plans/bay-api.md`](plans/bay-api.md) | REST API 契约 |
| [`plans/bay-concepts.md`](plans/bay-concepts.md) | 核心概念与职责边界 |
| [`plans/phase-1/phase-1.md`](plans/phase-1/phase-1.md) | Phase 1 进度摘要 |
| [`plans/phase-1/progress.md`](plans/phase-1/progress.md) | Phase 1 详细进度追踪 |
| [`plans/phase-1/gc-design.md`](plans/phase-1/gc-design.md) | GC 机制设计 |
| [`plans/phase-2/phase-2.md`](plans/phase-2/phase-2.md) | Phase 2 规划 |
| [`plans/phase-3/phase-3.md`](plans/phase-3/phase-3.md) | Phase 3 轻量化重构概览 |
| [`plans/phase-3/bay-go-design.md`](plans/phase-3/bay-go-design.md) | Bay Go 重写详细设计 |
| [`plans/ship-refactor-and-mcp.md`](plans/ship-refactor-and-mcp.md) | Ship MCP 集成设计 |

---

## 🧪 测试运行命令

```bash
# Bay 单元测试
cd pkgs/bay && uv run pytest tests/unit -v

# Bay E2E 测试 (docker-host 模式)
cd pkgs/bay && ./tests/scripts/docker-host/run.sh

# Bay E2E 测试 (docker-network 模式)
cd pkgs/bay && ./tests/scripts/docker-network/run.sh

# Ship 单元测试
cd pkgs/ship && uv run pytest tests/unit -v
```
