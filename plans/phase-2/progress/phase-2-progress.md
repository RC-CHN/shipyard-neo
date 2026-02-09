# Phase 2 Progress（实施进度记录）

> 更新时间：2026-02-09

本文档记录 Phase 2 已完成/进行中/待完成事项，方便在实现过程中快速对齐。

---

## 0. 当前结论/约定

- 浏览器运行时 **统一命名为 `gull`**（`runtime_type: gull`），不再使用 `runtime_type: browser`。
- Browser Runtime 通过 **CLI passthrough** 暴露能力：Bay 调用 Gull 的 `POST /exec`，由 Gull 透传执行 `agent-browser`。
- **不单独暴露 screenshot capability**：通过 `agent-browser screenshot /workspace/xxx.png` 写入共享 Cargo Volume，再由 Ship 的 filesystem/download 拉回。

---

## 1. 已完成（Done）

### 1.1 Profile Schema V2（多容器配置）

- 实现 V2 配置模型（`ContainerSpec`/`StartupConfig`/`ProfileConfig` 兼容归一化）。
  - 代码：[`pkgs/bay/app/config.py`](pkgs/bay/app/config.py:1)
- 新增单测覆盖：legacy profile 自动归一化、多容器解析、primary_for 优先级、primary container 解析。
  - 测试：[`pkgs/bay/tests/unit/config/test_profile_v2.py`](pkgs/bay/tests/unit/config/test_profile_v2.py:1)

### 1.2 Session 模型（Phase 2 多容器字段 + 状态）

- 扩展 Session 模型：新增 `containers: JSON` 字段、增加 `DEGRADED` 状态，提供能力/endpoint 查询辅助方法。
  - 代码：[`pkgs/bay/app/models/session.py`](pkgs/bay/app/models/session.py:1)

### 1.3 现有 Session 启动逻辑对 V2 Profile 的兼容

- 让 `SessionManager` 不再依赖 legacy `profile.runtime_port/runtime_type` 直接字段，而是基于 `profile.get_primary_container()`。
  - 代码：[`pkgs/bay/app/managers/session/session.py`](pkgs/bay/app/managers/session/session.py:1)

### 1.4 DockerDriver（多容器编排 + 向后兼容）

- `DockerDriver.create()` 继续基于 `profile.get_primary_container()`，保持 Phase 1/2 兼容。
- 新增 Phase 2 多容器编排：Session-scoped network + 多容器 create/start/stop/destroy（失败全部回滚）。
  - 代码：[`pkgs/bay/app/drivers/docker/docker.py`](pkgs/bay/app/drivers/docker/docker.py:1)
  - 相关模型：[`pkgs/bay/app/drivers/base.py`](pkgs/bay/app/drivers/base.py:1)

### 1.5 Gull（浏览器运行时）包与镜像骨架

- 新增 Gull 包（uv 管理、可构建 wheel）：
  - 配置：[`pkgs/gull/pyproject.toml`](pkgs/gull/pyproject.toml:1)
  - 锁文件：[`pkgs/gull/uv.lock`](pkgs/gull/uv.lock:1)
- Gull FastAPI Thin Wrapper：
  - 入口：[`pkgs/gull/app/main.py`](pkgs/gull/app/main.py:1)
  - 端点：`POST /exec`、`GET /health`、`GET /meta`（meta 格式与 Ship 对齐）
  - `agent-browser` 持久化：使用 `--profile /workspace/.browser/profile`（Cargo Volume）
  - 命令解析：使用 `shlex.split()`，支持带引号参数
- Gull Dockerfile（node + agent-browser + chromium deps + uv sync --frozen）：
  - 镜像：[`pkgs/gull/Dockerfile`](pkgs/gull/Dockerfile:1)
- Gull 单测（不依赖 agent-browser 实际安装）：
  - 测试：[`pkgs/gull/tests/test_runner.py`](pkgs/gull/tests/test_runner.py:1)

### 1.6 Bay 侧 GullAdapter 接入

- 新增 GullAdapter（HTTP adapter，调用 Gull `/meta` 与 `/exec`）：
  - 代码：[`pkgs/bay/app/adapters/gull.py`](pkgs/bay/app/adapters/gull.py:1)
  - 导出：[`pkgs/bay/app/adapters/__init__.py`](pkgs/bay/app/adapters/__init__.py:1)
- CapabilityRouter 识别 `runtime_type == "gull"`：
  - 代码：[`pkgs/bay/app/router/capability/capability.py`](pkgs/bay/app/router/capability/capability.py:1)
- GullAdapter 单测：
  - 测试：[`pkgs/bay/tests/unit/adapters/test_gull_adapter.py`](pkgs/bay/tests/unit/adapters/test_gull_adapter.py:1)

### 1.7 文档同步（runtime_type 统一）

- 将 Phase 2 文档中的 `runtime_type: browser` 统一为 `runtime_type: gull`：
  - [`plans/phase-2/profile-schema-v2.md`](plans/phase-2/profile-schema-v2.md:1)
  - [`plans/phase-2/browser-integration-design.md`](plans/phase-2/browser-integration-design.md:1)

### 1.8 测试现状

- Bay unit tests：`298 passed`（最新已跑通，新增多容器 SessionManager 单测）
- Gull unit tests：`3 passed`（已跑通）

---

## 2. 进行中（In Progress）

（本阶段核心能力已完成：多容器编排 + 能力路由 + browser exec API；后续聚焦 E2E 与 SDK/MCP。）

---

## 3. 待完成（Todo / Next）

### 3.1 数据面：Session.containers 真正落库

✅ 已实现：Session 启动时写入 `containers` JSON（并保持 primary container 的 `container_id/endpoint` 向后兼容）。
- 代码：[`pkgs/bay/app/managers/session/session.py`](pkgs/bay/app/managers/session/session.py:1)

### 3.2 生命周期与异常策略（按 decision-points）

- 多容器创建失败：全部回滚（✅ 已在启动逻辑实现）
- 运行中某容器挂掉：Session 标记 `DEGRADED`，对应能力 503（待补：`refresh_status()` 多容器版 + GC/路由层对 DEGRADED 返回 503）
- idle 回收：全活跃计数（Session 级 last_activity）（已存在，但需要确认 browser exec 触发 touch）

### 3.3 API：Bay 对外暴露 browser exec

✅ 已实现 browser exec：
- 新增端点：`POST /v1/capabilities/{id}/browser/exec`
  - 代码：[`pkgs/bay/app/api/v1/capabilities.py`](pkgs/bay/app/api/v1/capabilities.py:1)
  - 依赖：[`pkgs/bay/app/api/dependencies.py`](pkgs/bay/app/api/dependencies.py:1)

### 3.4 集成测试 / E2E

- E2E：创建 `ship + gull` profile
  1) gull: open/snapshot/screenshot 写入 `/workspace`
  2) ship: 下载截图并用 python 读取图片尺寸

---

## 4. 运行方式（开发提示）

### Gull
- 安装依赖：`cd pkgs/gull && uv sync --group dev`
- 单测：`cd pkgs/gull && uv run python -m pytest -q`

### Bay
- 单测：`cd pkgs/bay && uv run python -m pytest tests/unit/ -q`
