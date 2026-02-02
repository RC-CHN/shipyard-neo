# Shipyard Neo 项目待办清单

> 更新日期：2026-01-31
> 
> 本文档追踪项目级别的待办事项和演进路线。详细设计请参考 [`plans/`](plans/) 目录。

## 📊 总体进度概览

```
Phase 1 (MVP)      [████████████████████░░░░] 85%
Phase 1.5 (P1)     [████░░░░░░░░░░░░░░░░░░░░] 15%
Phase 2            [░░░░░░░░░░░░░░░░░░░░░░░░]  0%
```

---

## ✅ Phase 1 - 已完成

### Bay 核心 (100%)

- [x] FastAPI 项目骨架搭建
- [x] SQLite 数据库 + SQLModel ORM
- [x] Sandbox/Session/Workspace 模型定义
- [x] DockerDriver 实现（支持 host_port/container_network 模式）
- [x] SandboxManager 生命周期管理
- [x] SessionManager + ensure_running（含 runtime readiness 等待）
- [x] WorkspaceManager（Docker Volume 后端）
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

## 🚨 P0 - 最高优先级：命名重构（Workspace → Locker）

> **决策**：将 Workspace 重命名为 Locker，延续航海拟物化命名风格
>
> **理由**：在继续开发新功能之前完成重命名，避免后续更大范围的改动

**命名体系**：
```
🏖️ Bay    - 港湾 (管理层，调度中心)
🚢 Ship   - 船 (运行时，计算载体)
🔐 Locker - 储物柜 (数据持久化，安全存储)
```

**重命名范围**：

- [ ] **设计文档更新**
  - [ ] `plans/bay-design.md` - 概念模型中 Workspace → Locker
  - [ ] `plans/bay-concepts.md` - 数据概念更新
  - [ ] `plans/bay-api.md` - API 路径更新 `/workspaces` → `/lockers`
  - [ ] `plans/phase-1/*.md` - 相关引用更新
- [ ] **Bay 代码重构**
  - [ ] `pkgs/bay/app/models/workspace.py` → `locker.py`
  - [ ] `pkgs/bay/app/managers/workspace/` → `locker/`
  - [ ] API 路由 `/v1/workspaces` → `/v1/lockers`
  - [ ] 数据库表名 `workspaces` → `lockers`
  - [ ] 字段名 `workspace_id` → `locker_id`
  - [ ] `managed_by_sandbox_id` 保持不变
- [ ] **Ship 代码更新**
  - [ ] `pkgs/ship/app/workspace.py` 更新引用
  - [ ] 挂载路径保持 `/workspace`（内部实现细节，不对外暴露）
- [ ] **测试更新**
  - [ ] 单元测试文件和用例更新
  - [ ] E2E 测试更新
- [ ] **SDK 更新**
  - [ ] `sdk-reference/` 中的引用更新
- [ ] **README 和文档更新**
  - [ ] 根目录 README.md
  - [ ] 各子包 README.md

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

- [ ] **IdleSessionGC**：空闲 Session 回收（idle_expires_at 过期）
- [ ] **ExpiredSandboxGC**：过期 Sandbox 清理（expires_at 过期）
- [ ] **OrphanWorkspaceGC**：孤儿 managed workspace 清理
- [ ] **OrphanContainerGC**：孤儿容器检测与清理
- [ ] GC 调度器框架（GCTask + GCScheduler）
- [ ] 启动时 reconcile
- [ ] 配置化 GC 间隔与开关

### ✅ Extend TTL (已完成)

> 详见 [`plans/phase-1/gc-design.md#8.3`](plans/phase-1/gc-design.md)

- [x] `POST /v1/sandboxes/{id}/extend_ttl` API 实现
- [x] expires_at 计算规则（max(old, now) + extend_by）
- [x] 拒绝复活已过期 Sandbox (409 `sandbox_expired`)
- [x] 拒绝延长 TTL=null 的 Sandbox (409 `sandbox_ttl_infinite`)
- [x] Idempotency-Key 支持
- [x] E2E 测试覆盖 (`test_extend_ttl.py`, `test_long_running_extend_ttl.py`)

### 🟠 中优先级：Locker API

> 详见 [`plans/bay-api.md#6.3`](plans/bay-api.md)（重命名后）

- [ ] `POST /v1/lockers` - 创建独立 Locker
- [ ] `GET /v1/lockers` - 列出 Lockers
- [ ] `GET /v1/lockers/{id}` - 查询 Locker
- [ ] `DELETE /v1/lockers/{id}` - 删除 Locker
- [ ] `POST /v1/lockers/{id}/files/read` - 直读文件
- [ ] `POST /v1/lockers/{id}/files/write` - 直写文件
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
