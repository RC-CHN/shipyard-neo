# Gull 浏览器运行时操作文档

> 🦅 Gull (海鸥) — 航行在 Bay 港湾中的浏览器运行时
>
> 源码位置: [`pkgs/gull/`](pkgs/gull/README.md)

## 目录

- [1. 概述](#1-概述)
- [2. 架构设计](#2-架构设计)
- [3. REST API 参考](#3-rest-api-参考)
- [4. 与 Bay 集成](#4-与-bay-集成)
- [5. 开发指南](#5-开发指南)
- [6. Docker 部署](#6-docker-部署)
- [7. 常见工作流](#7-常见工作流)
- [8. 配置参考](#8-配置参考)
- [9. 故障排除](#9-故障排除)

---

## 1. 概述

Gull 是 Shipyard Neo 的**浏览器运行时组件**，作为 [`agent-browser`](https://github.com/anthropics/agent-browser) CLI 工具的 HTTP 代理运行在 Docker 容器中。它通过 **CLI Passthrough** 模式将 agent-browser 的 50+ 命令暴露为单一 REST API，无需逐一封装每个浏览器命令。

### 核心特性

| 特性 | 说明 |
|------|------|
| **CLI 透传** | 将 agent-browser 命令原样透传执行，支持所有 50+ 子命令 |
| **会话隔离** | 自动注入 `--session` 参数，按 Sandbox ID 隔离浏览器实例 |
| **状态持久化** | 自动注入 `--profile` 参数，将 cookies/localStorage 等持久化到 Cargo Volume |
| **批量执行** | 支持 `/exec_batch` 端点，一次请求执行多条命令 |
| **超时控制** | 每条命令支持独立超时，批量执行支持整体超时预算 |
| **学习证据** | Bay 层可选记录 `execution_id/trace_ref`，支持 browser skill 自动学习与回放 |

### 技术栈

- **运行时**: Python 3.11 + [FastAPI](https://fastapi.tiangolo.com/)
- **CLI 代理**: [agent-browser](https://github.com/anthropics/agent-browser) (Node.js)
- **浏览器引擎**: Playwright Chromium (headless)
- **进程管理**: `asyncio.create_subprocess_exec` (非阻塞)

---

## 2. 架构设计

### 2.1 容器架构

```
┌──────────────────────────────────────────────────────┐
│                 Gull Container                         │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │           FastAPI REST Wrapper                    │ │
│  │           (app.main:app, port 8080)               │ │
│  │                                                    │ │
│  │  POST /exec       → 单条命令执行                  │ │
│  │  POST /exec_batch → 批量命令执行                  │ │
│  │  GET  /health     → 健康检查                      │ │
│  │  GET  /meta       → 运行时元数据                  │ │
│  └──────────────────────────────────────────────────┘ │
│                       │                                │
│                       ▼                                │
│  ┌──────────────────────────────────────────────────┐ │
│  │          agent-browser CLI (Node.js)              │ │
│  │                                                    │ │
│  │  自动注入:                                         │ │
│  │  --session $SANDBOX_ID  → 浏览器实例隔离           │ │
│  │  --profile /workspace/.browser/profile/ → 状态持久 │ │
│  └──────────────────────────────────────────────────┘ │
│                       │                                │
│                       ▼                                │
│  ┌──────────────────────────────────────────────────┐ │
│  │          Playwright Chromium (headless)            │ │
│  └──────────────────────────────────────────────────┘ │
│                       │                                │
│                       ▼                                │
│           /workspace (Cargo Volume, 共享存储)          │
│           /workspace/.browser/profile/ (浏览器状态)    │
└──────────────────────────────────────────────────────┘
```

### 2.2 在 Shipyard Neo 中的位置

Gull 作为 Sandbox 的 sidecar 容器运行，与 Ship（代码执行运行时）共享同一个 Cargo Volume：

```
┌─────────────────────────────────────────────────────────┐
│                      Sandbox                              │
│                                                           │
│  ┌─────────────────┐         ┌─────────────────────────┐ │
│  │  Ship Container  │         │    Gull Container        │ │
│  │  (代码执行)      │         │    (浏览器自动化)        │ │
│  │                  │         │                          │ │
│  │  Python/Shell    │         │  agent-browser           │ │
│  │  文件系统操作    │         │  Playwright Chromium     │ │
│  └────────┬─────────┘         └────────────┬─────────────┘ │
│           │                                │               │
│           └──────────┬─────────────────────┘               │
│                      ▼                                     │
│           ┌──────────────────────┐                         │
│           │   Cargo Volume        │                         │
│           │   /workspace          │                         │
│           │                       │                         │
│           │   代码文件/截图/数据  │                         │
│           │   .browser/profile/   │                         │
│           └──────────────────────┘                         │
└─────────────────────────────────────────────────────────┘
```

**关键设计要点**:

- Ship 和 Gull **共享 Cargo Volume**(`/workspace`)，意味着浏览器截图保存到 `/workspace/xxx.png` 后，可直接通过 Ship 的 filesystem capability 下载
- 浏览器状态（cookies、localStorage、IndexedDB 等）持久化在 `/workspace/.browser/profile/`，随 Cargo Volume 一起存在
- Sandbox 删除时，Cargo Volume 一并清理，浏览器状态自动回收

### 2.3 命令执行流程

```
Bay API 请求
  │
  │ POST /v1/sandboxes/{id}/browser/exec
  │ {"cmd": "open https://example.com"}
  │
  ▼
Bay CapabilityRouter
  │  根据 capability="browser" 路由到 GullAdapter
  ▼
GullAdapter.exec_browser()
  │  POST http://gull-container:8080/exec
  │  {"cmd": "open https://example.com", "timeout": 30}
  ▼
Gull FastAPI 服务
  │  构建完整命令:
  │  agent-browser --session $SANDBOX_ID \
  │                --profile /workspace/.browser/profile/ \
  │                open https://example.com
  ▼
asyncio.create_subprocess_exec()
  │  非阻塞执行 agent-browser CLI
  ▼
agent-browser → Playwright Chromium
  │  执行浏览器操作
  ▼
返回 {stdout, stderr, exit_code}
```

### 2.4 会话与状态管理

Gull 的会话管理依赖 agent-browser 的两个关键参数：

| 参数 | 来源 | 作用 |
|------|------|------|
| `--session` | 环境变量 `SANDBOX_ID` / `BAY_SANDBOX_ID` | 隔离浏览器进程实例，不同 Sandbox 互不干扰 |
| `--profile` | 固定路径 `/workspace/.browser/profile/` | 持久化浏览器状态到 Cargo Volume |

**持久化内容**（由 agent-browser `--profile` 自动管理）:
- Cookies
- localStorage / sessionStorage
- IndexedDB
- Service Workers
- 浏览器缓存

**生命周期**:

| 事件 | 行为 |
|------|------|
| Gull 容器启动 | 创建 profile 目录，执行 `open about:blank` 预热 Chromium，agent-browser 自动恢复已持久化状态 |
| Gull 容器关闭 | 调用 `agent-browser close` 关闭浏览器，状态自动持久化 |
| Sandbox 删除 | Cargo Volume 删除，浏览器状态一并清理 |

> **浏览器预热**: Gull 在 lifespan 启动阶段执行 `agent-browser open about:blank` 触发 Chromium 初始化。预热成功后 `/health` 的 `browser_ready` 字段变为 `true`。预热失败不阻止服务启动，但 `browser_ready` 保持 `false`，Bay 会在 `_wait_for_ready()` 中持续等待直到超时。

---

## 3. REST API 参考

> 源码: [`app/main.py`](pkgs/gull/app/main.py)

Gull 暴露 4 个 HTTP 端点，所有端点均在 `0.0.0.0:8080` 上监听。

### 3.1 执行命令 — `POST /exec`

执行单条 agent-browser 命令。这是 Gull 的核心端点，所有浏览器操作都通过此端点完成。

**请求体** ([`ExecRequest`](pkgs/gull/app/main.py:40)):

| 字段 | 类型 | 默认值 | 约束 | 说明 |
|------|------|--------|------|------|
| `cmd` | string | *必填* | — | agent-browser 命令（不含 `agent-browser` 前缀） |
| `timeout` | int | `30` | 1-300 | 超时秒数 |

**请求示例**:

```bash
# 导航到网页
curl -X POST http://localhost:8080/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "open https://example.com"}'

# 获取交互元素快照
curl -X POST http://localhost:8080/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "snapshot -i"}'

# 点击元素（使用 snapshot 返回的 ref）
curl -X POST http://localhost:8080/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "click @e1"}'

# 填写表单（引号内的字符串作为一个参数）
curl -X POST http://localhost:8080/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "fill @e2 \"hello world\""}'

# 截图保存到工作区
curl -X POST http://localhost:8080/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "screenshot /workspace/page.png"}'
```

**响应** `200` ([`ExecResponse`](pkgs/gull/app/main.py:49)):

```json
{
  "stdout": "Navigated to https://example.com",
  "stderr": "",
  "exit_code": 0
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `stdout` | string | 命令标准输出 |
| `stderr` | string | 命令标准错误 |
| `exit_code` | int | 退出码，`0` 表示成功，`-1` 表示超时 |

**错误情况**:

| 场景 | exit_code | stderr |
|------|-----------|--------|
| 命令超时 | `-1` | `"Command timed out after {N}s"` |
| agent-browser 未安装 | `-1` | `"agent-browser not found. Is it installed?"` |
| 命令执行异常 | `-1` | `"Failed to execute command: {error}"` |
| 无效子命令 | 非零 | agent-browser 原始错误信息 |
| 请求体缺少 `cmd` | HTTP `422` | FastAPI 验证错误 |
| timeout 超出范围 | HTTP `422` | FastAPI 验证错误 |

### 3.2 批量执行 — `POST /exec_batch`

按顺序执行一批 agent-browser 命令。适用于需要多步操作的场景（如：打开页面 → 等待加载 → 获取快照）。

**请求体** ([`BatchExecRequest`](pkgs/gull/app/main.py:57)):

| 字段 | 类型 | 默认值 | 约束 | 说明 |
|------|------|--------|------|------|
| `commands` | list[string] | *必填* | 至少 1 条 | 命令列表（不含前缀） |
| `timeout` | int | `60` | 1-600 | 整批超时秒数 |
| `stop_on_error` | bool | `true` | — | 遇到错误是否停止 |

**请求示例**:

```bash
curl -X POST http://localhost:8080/exec_batch \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      "open https://example.com",
      "wait --load networkidle",
      "snapshot -i"
    ],
    "timeout": 60,
    "stop_on_error": true
  }'
```

**响应** `200` ([`BatchExecResponse`](pkgs/gull/app/main.py:80)):

```json
{
  "results": [
    {
      "cmd": "open https://example.com",
      "stdout": "Navigated to https://example.com",
      "stderr": "",
      "exit_code": 0,
      "step_index": 0,
      "duration_ms": 1200
    },
    {
      "cmd": "wait --load networkidle",
      "stdout": "",
      "stderr": "",
      "exit_code": 0,
      "step_index": 1,
      "duration_ms": 350
    },
    {
      "cmd": "snapshot -i",
      "stdout": "@e1 [a] \"More information...\"",
      "stderr": "",
      "exit_code": 0,
      "step_index": 2,
      "duration_ms": 200
    }
  ],
  "total_steps": 3,
  "completed_steps": 3,
  "success": true,
  "duration_ms": 1750
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `results` | list | 每步执行结果（[`BatchStepResult`](pkgs/gull/app/main.py:69)） |
| `total_steps` | int | 请求中的总步数 |
| `completed_steps` | int | 实际完成的步数（`stop_on_error=true` 时可能 < total） |
| `success` | bool | 所有已执行步骤是否全部成功 |
| `duration_ms` | int | 整批执行总耗时（毫秒） |

**超时预算机制**:

批量执行使用**剩余超时预算**策略——每执行完一步，从整体 timeout 中扣除已用时间。如果剩余预算不足，后续步骤的超时值会自动缩短（最小 1 秒）。

### 3.3 健康检查 — `GET /health`

检查 Gull 服务和浏览器的运行状态。同时作为 liveness 和 readiness 探针。

**请求示例**:

```bash
curl http://localhost:8080/health
```

**响应** `200` ([`HealthResponse`](pkgs/gull/app/main.py:90)):

```json
{
  "status": "healthy",
  "browser_active": true,
  "browser_ready": true,
  "session": "sbx_abc123"
}
```

| 字段 | 类型 | 可选值 | 说明 |
|------|------|--------|------|
| `status` | string | `healthy` / `degraded` / `unhealthy` | 服务状态（liveness） |
| `browser_active` | bool | — | 当前 session 是否有活跃浏览器 |
| `browser_ready` | bool | — | 浏览器是否已预热就绪（readiness） |
| `session` | string | — | 当前 session 名称（来自 `SANDBOX_ID`） |

**状态判断逻辑**（见 [`health()`](pkgs/gull/app/main.py:301)）:

| 条件 | 返回状态 |
|------|----------|
| `agent-browser` 命令可用 | `healthy` |
| `agent-browser` 命令不存在 | `unhealthy` |

`browser_active` 通过执行 `agent-browser session list` 并检查当前 session 名称是否在输出中来判断。

**`browser_ready` 字段**:

`browser_ready` 表示 Chromium 浏览器是否已在 lifespan 启动阶段完成预热。Gull 在启动时会执行 `agent-browser open about:blank` 来触发 Chromium 初始化，成功后将 `browser_ready` 设为 `true`。

Bay 的 `SessionManager._wait_for_ready()` 在等待 browser 类型容器就绪时，会检查此字段。只有当 `browser_ready=true` 时，Bay 才会标记 session 为 `RUNNING`。这确保了用户发送第一个 `open` 命令时浏览器已完全可用，无需等待 Chromium 冷启动的额外延迟。

> **向后兼容**: 旧版 Gull 镜像的 `/health` 响应不包含 `browser_ready` 字段。Bay 在解析时使用 `payload.get("browser_ready", True)` 作为默认值，因此旧版 Gull 会被视为就绪，不会阻塞。

### 3.4 运行时元数据 — `GET /meta`

返回 Gull 运行时的版本和能力信息，供 Bay 的 [`CapabilityRouter`](pkgs/bay/app/router/capability/capability.py) 使用。

**请求示例**:

```bash
curl http://localhost:8080/meta
```

**响应** `200` ([`MetaResponse`](pkgs/gull/app/main.py:98)):

```json
{
  "runtime": {
    "name": "gull",
    "version": "0.1.0",
    "api_version": "v1"
  },
  "workspace": {
    "mount_path": "/workspace"
  },
  "capabilities": {
    "browser": {"version": "1.0"}
  }
}
```

| 字段 | 说明 |
|------|------|
| `runtime.name` | 运行时名称（固定 `"gull"`） |
| `runtime.version` | Gull 版本号 |
| `runtime.api_version` | API 版本（固定 `"v1"`） |
| `workspace.mount_path` | 工作区挂载路径 |
| `capabilities` | 支持的能力列表，当前仅 `browser` |

> **注意**: screenshot **不是**独立的 capability。截图通过 `agent-browser screenshot /workspace/xxx.png` 命令保存到共享 Cargo Volume，然后通过 Ship 的 filesystem capability 下载。

### 3.5 支持的 agent-browser 命令速查

以下命令均可通过 `POST /exec` 的 `cmd` 字段执行（不含 `agent-browser` 前缀）：

| 分类 | 命令 | 示例 |
|------|------|------|
| **导航** | `open <url>` | `"open https://example.com"` |
| | `reload` | `"reload"` |
| | `back` / `forward` | `"back"` |
| | `close` | `"close"` |
| **快照** | `snapshot` | `"snapshot"` — 完整可访问性树 |
| | `snapshot -i` | `"snapshot -i"` — 仅交互元素（推荐） |
| | `snapshot -c` | `"snapshot -c"` — 紧凑模式 |
| | `snapshot -i -C` | `"snapshot -i -C"` — 含 cursor 交互元素 |
| **交互** | `click @ref` | `"click @e1"` |
| | `fill @ref "text"` | `"fill @e2 \"hello\""` |
| | `type @ref "text"` | `"type @e2 \"hello\""` — 不清空 |
| | `select @ref "option"` | `"select @e3 \"California\""` |
| | `check @ref` | `"check @e4"` |
| | `press <key>` | `"press Enter"` |
| | `scroll <direction> <amount>` | `"scroll down 500"` |
| **信息获取** | `get text @ref` | `"get text @e1"` |
| | `get url` | `"get url"` |
| | `get title` | `"get title"` |
| | `get count "selector"` | `"get count \"a\""` |
| **JavaScript** | `eval <expression>` | `"eval document.title"` |
| **截图** | `screenshot [path]` | `"screenshot /workspace/page.png"` |
| | `screenshot --full` | `"screenshot --full"` |
| **等待** | `wait @ref` | `"wait @e1"` |
| | `wait --load networkidle` | `"wait --load networkidle"` |
| | `wait --url "pattern"` | `"wait --url \"**/dashboard\""` |
| | `wait <ms>` | `"wait 2000"` |
| **会话** | `session list` | `"session list"` |

> **重要**: Ref（`@e1`、`@e2` 等）在页面导航后会失效，必须重新执行 `snapshot -i` 获取新的 ref。

---

## 4. 与 Bay 集成

> 相关源码: [`GullAdapter`](pkgs/bay/app/adapters/gull.py:41)、[`CapabilityRouter`](pkgs/bay/app/router/capability/capability.py)

### 4.1 GullAdapter

Bay 通过 [`GullAdapter`](pkgs/bay/app/adapters/gull.py:41) 与 Gull 容器通信。Adapter 实现了 [`BaseAdapter`](pkgs/bay/app/adapters/base.py) 接口，提供以下方法：

| 方法 | 说明 | Gull 端点 |
|------|------|-----------|
| [`exec_browser()`](pkgs/bay/app/adapters/gull.py:162) | 执行单条浏览器命令 | `POST /exec` |
| [`exec_browser_batch()`](pkgs/bay/app/adapters/gull.py:201) | 批量执行浏览器命令 | `POST /exec_batch` |
| [`get_meta()`](pkgs/bay/app/adapters/gull.py:117) | 获取运行时元数据（带缓存） | `GET /meta` |
| [`health()`](pkgs/bay/app/adapters/gull.py:144) | 健康检查 | `GET /health` |

**超时策略**:

GullAdapter 在转发请求时会在 Gull 的 timeout 基础上增加额外余量，确保 HTTP 超时晚于命令超时：

| 操作 | Gull timeout | HTTP timeout |
|------|-------------|--------------|
| 单条执行 | `N` 秒 | `N + 5` 秒 |
| 批量执行 | `N` 秒 | `N + 10` 秒 |
| Meta 查询 | — | `5` 秒 |
| 健康检查 | — | `5` 秒 |

**结果映射**:

[`exec_browser()`](pkgs/bay/app/adapters/gull.py:162) 将 Gull 的原始响应映射为 [`ExecutionResult`](pkgs/bay/app/adapters/base.py):

```python
ExecutionResult(
    success = (exit_code == 0),
    output  = stdout,     # 标准输出 → output
    error   = stderr,     # 标准错误 → error
    exit_code = exit_code,
    data    = {"raw": raw_response},  # 保留完整原始响应
)
```

### 4.2 Capability 路由

Bay 的 CapabilityRouter 根据请求的 capability 类型将请求路由到对应的 Adapter：

| Capability | Adapter | 运行时 |
|------------|---------|--------|
| `python` | ShipAdapter | Ship |
| `shell` | ShipAdapter | Ship |
| `filesystem` | ShipAdapter | Ship |
| **`browser`** | **GullAdapter** | **Gull** |

### 4.3 Bay API 调用方式

通过 Bay API 使用浏览器功能：

**单条命令**（见 [Bay API 2.3](bay_api_v1.md#23-浏览器命令执行)）:

```bash
curl -X POST http://bay-server/v1/sandboxes/{sandbox_id}/browser/exec \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "cmd": "open https://example.com",
    "timeout": 30,
    "description": "search homepage",
    "tags": "browser,search",
    "learn": true,
    "include_trace": true
  }'
```

```json
{
  "success": true,
  "output": "Navigated to https://example.com",
  "error": null,
  "exit_code": 0,
  "execution_id": "exec-xxx",
  "execution_time_ms": 1240,
  "trace_ref": "blob:blob-xxx"
}
```

**批量命令**（见 [Bay API 2.4](bay_api_v1.md#24-浏览器批量执行)）:

```bash
curl -X POST http://bay-server/v1/sandboxes/{sandbox_id}/browser/exec_batch \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      "open https://example.com",
      "wait --load networkidle",
      "snapshot -i",
      "screenshot /workspace/page.png"
    ],
    "timeout": 60,
    "stop_on_error": true,
    "learn": true,
    "include_trace": true
  }'
```

**Browser skill 回放**:

```bash
curl -X POST http://bay-server/v1/sandboxes/{sandbox_id}/browser/skills/{skill_key}/run \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{"timeout": 60, "include_trace": true}'
```

**Trace 查询**:

```bash
curl -X GET http://bay-server/v1/sandboxes/{sandbox_id}/browser/traces/{trace_ref} \
  -H 'Authorization: Bearer <token>'
```

### 4.4 Profile 配置

要在 Sandbox 中启用浏览器功能，需要使用包含 `browser` capability 的 Profile。多容器 Profile 配置示例（见 [`config.yaml`](deploy/docker/config.yaml)）：

```yaml
profiles:
  browser-enabled:
    description: "Python + Shell + Browser"
    containers:
      - name: ship
        runtime_type: ship
        image: "shipyard/ship:latest"
        capabilities: [python, shell, filesystem]
        resources:
          cpus: 1.0
          memory: "1g"
      - name: gull
        runtime_type: gull
        image: "shipyard/gull:latest"
        capabilities: [browser]
        resources:
          cpus: 0.5
          memory: "512m"
    idle_timeout: 600
```

使用该 Profile 创建 Sandbox：

```bash
curl -X POST http://bay-server/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"profile": "browser-enabled"}'
```

### 4.5 截图工作流（跨容器协作）

浏览器截图涉及 Gull 和 Ship 两个容器的协作：

```
1. 通过 Gull 截图并保存到 Cargo Volume
   POST /v1/sandboxes/{id}/browser/exec
   {"cmd": "screenshot /workspace/screenshots/page.png"}

2. 通过 Ship 的 filesystem capability 下载截图
   GET /v1/sandboxes/{id}/filesystem/download?path=screenshots/page.png
```

这是因为 Ship 和 Gull 共享同一个 Cargo Volume (`/workspace`)，Gull 保存的文件对 Ship 即时可见。

---

## 5. 开发指南

### 5.1 项目结构

```
pkgs/gull/
├── Dockerfile              # 容器镜像构建
├── pyproject.toml           # Python 项目配置
├── README.md                # 项目简介
├── uv.lock                  # 依赖锁定
├── app/
│   ├── __init__.py
│   └── main.py              # FastAPI 应用（全部逻辑集中在单文件）
└── tests/
    ├── integration/
    │   ├── conftest.py       # 集成测试配置（Docker 容器管理）
    │   └── test_gull_api.py  # HTTP API 集成测试
    └── unit/
        └── test_runner.py    # 命令执行器单元测试
```

### 5.2 本地开发

```bash
# 进入项目目录
cd pkgs/gull

# 安装依赖
uv sync

# 启动开发服务（热重载）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

> **注意**: 本地开发需要安装 `agent-browser` 和 Playwright Chromium：
> ```bash
> npm install -g agent-browser
> npx playwright install --with-deps chromium
> ```

### 5.3 运行测试

**单元测试**（不依赖 agent-browser 安装）：

```bash
cd pkgs/gull
uv run pytest tests/unit/ -v
```

单元测试通过 monkeypatch 模拟 `asyncio.create_subprocess_exec`，验证：
- `--session` 和 `--profile` 参数注入（[`test_run_agent_browser_injects_session_and_profile`](pkgs/gull/tests/unit/test_runner.py:39)）
- 引号参数保留（[`test_run_agent_browser_preserves_quoted_args`](pkgs/gull/tests/unit/test_runner.py:77)）
- 超时进程终止（[`test_run_agent_browser_timeout_kills_process`](pkgs/gull/tests/unit/test_runner.py:103)）

**集成测试**（需要 Docker 和 `gull:latest` 镜像）：

```bash
# 先构建镜像
docker build -t gull:latest pkgs/gull/

# 运行集成测试
cd pkgs/gull
uv run pytest tests/integration/ -v
```

集成测试覆盖（[`test_gull_api.py`](pkgs/gull/tests/integration/test_gull_api.py)）：

| 测试类 | 覆盖内容 |
|--------|----------|
| `TestHealthEndpoint` | `/health` 返回码、响应结构、状态值 |
| `TestMetaEndpoint` | `/meta` 返回码、结构、运行时信息、能力声明 |
| `TestExecValidation` | 请求体校验、`--version` 基础命令 |
| `TestNavigation` | `open`、`get title`、`get url`、`reload` |
| `TestSnapshot` | `snapshot`、`snapshot -i`、`snapshot -c` |
| `TestGetInfo` | `get text`、`get count` |
| `TestJavaScript` | `eval` 表达式执行 |
| `TestScreenshot` | 截图保存到 `/workspace` |
| `TestSession` | `session list` |
| `TestScroll` | `scroll down` |
| `TestErrorHandling` | 无效命令错误码 |

### 5.4 代码要点

**引号参数处理**（[`_run_agent_browser()`](pkgs/gull/app/main.py:106)）:

Gull 使用 `shlex.split()` 解析命令字符串，确保带引号的参数被正确拆分：

```python
# cmd = 'fill @e1 "hello world"'
# shlex.split(cmd) → ['fill', '@e1', 'hello world']
parts.extend(shlex.split(cmd))
```

**事件循环注意事项**（[`Dockerfile CMD`](pkgs/gull/Dockerfile:49)）:

启动命令使用 `--loop asyncio` 强制标准 asyncio 事件循环，而非 uvloop：

```
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "asyncio"]
```

原因：uvloop 的管道管理与 Node.js 子进程（agent-browser）不兼容，会导致 `process.communicate()` 挂起等待 EOF，表现为命令超时。

### 5.5 Bay Browser Self-Iteration 回归建议

Gull 改动后，建议在 Bay 侧补跑 browser 自迭代回归，确保 `learn/include_trace` 与证据链路不回退：

```bash
cd pkgs/bay
uv run pytest -q \
  tests/integration/core/test_history_api.py \
  tests/integration/core/test_browser_skill_e2e.py
```

重点核对：

- `POST /v1/sandboxes/{sandbox_id}/browser/exec` 与 `exec_batch` 都返回 `execution_id`。
- `learn=true` 时 history 中存在 `learn_enabled/payload_ref`。
- `include_trace=true` 时响应返回 `trace_ref`，且可通过 `GET /browser/traces/{trace_ref}` 取回轨迹。
- `POST /browser/skills/{skill_key}/run` 在无 active release 时返回明确 404。

---

## 6. Docker 部署

### 6.1 构建镜像

```bash
docker build -t gull:latest pkgs/gull/
```

镜像构建过程（[`Dockerfile`](pkgs/gull/Dockerfile)）：

| 层 | 内容 |
|----|------|
| 基础镜像 | `uv:0.5.30-python3.11-bookworm-slim` |
| 系统依赖 | Node.js 20.x（agent-browser 运行时） |
| 浏览器工具 | `agent-browser` (npm global) + Playwright + Chromium |
| Python 依赖 | FastAPI、uvicorn、pydantic（通过 `uv sync`） |

### 6.2 独立运行

```bash
docker run -d \
  --name gull \
  -p 8080:8080 \
  -v my-workspace:/workspace \
  -e SANDBOX_ID=test-session \
  gull:latest
```

### 6.3 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SANDBOX_ID` | `"default"` | 浏览器会话名称（`--session` 参数值） |
| `BAY_SANDBOX_ID` | — | `SANDBOX_ID` 的备选变量名 |
| `BAY_WORKSPACE_PATH` | `"/workspace"` | 工作区挂载路径 |

优先级：`SANDBOX_ID` > `BAY_SANDBOX_ID` > `"default"`

### 6.4 端口和存储

| 资源 | 值 | 说明 |
|------|-----|------|
| HTTP 端口 | `8080` | FastAPI 服务 |
| 工作区路径 | `/workspace` | Cargo Volume 挂载点 |
| 浏览器 Profile | `/workspace/.browser/profile/` | 浏览器状态持久化目录 |

### 6.5 健康检查示例

```bash
# 检查服务是否就绪
curl -sf http://localhost:8080/health | jq .

# Docker 健康检查配置
docker run -d \
  --health-cmd "curl -sf http://localhost:8080/health || exit 1" \
  --health-interval 10s \
  --health-timeout 5s \
  --health-retries 3 \
  gull:latest
```

---

## 7. 常见工作流

### 7.1 网页内容提取

```
# 步骤 1: 打开页面并等待加载
POST /exec {"cmd": "open https://example.com"}
POST /exec {"cmd": "wait --load networkidle"}

# 步骤 2: 获取交互元素快照
POST /exec {"cmd": "snapshot -i"}
# 输出: @e1 [a] "More information..."

# 步骤 3: 提取文本内容
POST /exec {"cmd": "get text @e1"}
POST /exec {"cmd": "get title"}

# 或使用批量执行
POST /exec_batch {
  "commands": [
    "open https://example.com",
    "wait --load networkidle",
    "snapshot -i",
    "get title"
  ]
}
```

### 7.2 表单填写与提交

```
# 步骤 1: 打开表单页面
POST /exec {"cmd": "open https://example.com/signup"}

# 步骤 2: 获取表单元素
POST /exec {"cmd": "snapshot -i"}
# 输出:
# @e1 [input type="text"] placeholder="Name"
# @e2 [input type="email"] placeholder="Email"
# @e3 [select] "Country"
# @e4 [input type="checkbox"] "Agree to terms"
# @e5 [button] "Submit"

# 步骤 3: 填写并提交
POST /exec {"cmd": "fill @e1 \"Jane Doe\""}
POST /exec {"cmd": "fill @e2 \"jane@example.com\""}
POST /exec {"cmd": "select @e3 \"China\""}
POST /exec {"cmd": "check @e4"}
POST /exec {"cmd": "click @e5"}
POST /exec {"cmd": "wait --load networkidle"}

# 步骤 4: 验证结果（重新获取快照，ref 已失效）
POST /exec {"cmd": "snapshot -i"}
```

### 7.3 截图并下载

```
# 步骤 1: 通过 Gull 导航并截图
POST /v1/sandboxes/{id}/browser/exec
  {"cmd": "open https://example.com"}

POST /v1/sandboxes/{id}/browser/exec
  {"cmd": "screenshot /workspace/screenshots/page.png"}

# 全页截图
POST /v1/sandboxes/{id}/browser/exec
  {"cmd": "screenshot --full /workspace/screenshots/full-page.png"}

# 步骤 2: 通过 Ship 的 filesystem capability 下载
GET /v1/sandboxes/{id}/filesystem/download?path=screenshots/page.png
```

### 7.4 JavaScript 执行

```
# 获取页面标题
POST /exec {"cmd": "eval document.title"}

# 获取所有链接
POST /exec {"cmd": "eval document.querySelectorAll('a').length"}

# 执行自定义脚本
POST /exec {"cmd": "eval window.scrollTo(0, document.body.scrollHeight)"}
```

### 7.5 带登录状态的持久化操作

由于 Gull 自动使用 `--profile` 持久化浏览器状态，登录后的 cookies/session 会在容器重启后自动恢复：

```
# 首次：登录操作
POST /exec {"cmd": "open https://app.example.com/login"}
POST /exec {"cmd": "snapshot -i"}
POST /exec {"cmd": "fill @e1 \"user@example.com\""}
POST /exec {"cmd": "fill @e2 \"password123\""}
POST /exec {"cmd": "click @e3"}
POST /exec {"cmd": "wait --url \"**/dashboard\""}

# 容器重启后：直接访问（已持久化的 cookies 自动生效）
POST /exec {"cmd": "open https://app.example.com/dashboard"}
# 应该能直接进入 dashboard 而不需要重新登录
```

---

## 8. 配置参考

### 8.1 Gull 内部配置

Gull 的配置通过环境变量和代码常量定义（见 [`main.py`](pkgs/gull/app/main.py:30)）：

| 配置项 | 来源 | 默认值 | 说明 |
|--------|------|--------|------|
| Session 名称 | `SANDBOX_ID` / `BAY_SANDBOX_ID` | `"default"` | 浏览器会话隔离标识 |
| 工作区路径 | `BAY_WORKSPACE_PATH` | `"/workspace"` | 共享存储挂载路径 |
| Profile 目录 | 固定 | `{workspace}/.browser/profile/` | 浏览器状态持久化 |
| 服务端口 | 固定 | `8080` | HTTP 服务端口 |
| 版本号 | 固定 | `"0.1.0"` | Gull 版本 |

### 8.2 依赖版本

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | ≥ 3.11 | 运行时 |
| FastAPI | ≥ 0.115.0 | HTTP 框架 |
| uvicorn | ≥ 0.30.0 | ASGI 服务器 |
| pydantic | ≥ 2.0 | 数据校验 |
| Node.js | 20.x | agent-browser 运行时 |
| agent-browser | latest | 浏览器自动化 CLI |
| Playwright | latest | 浏览器引擎 |

### 8.3 资源建议

| 资源 | 最低 | 建议 | 说明 |
|------|------|------|------|
| CPU | 0.25 核 | 0.5 核 | Chromium 渲染需要计算资源 |
| 内存 | 256MB | 512MB | Chromium + Node.js + Python |
| 磁盘 | — | — | 依赖 Cargo Volume |

---

## 9. 故障排除

### 9.1 常见问题

#### 命令超时

**症状**: `exit_code: -1`，`stderr: "Command timed out after {N}s"`

**可能原因**:
1. 页面加载过慢 → 增大 `timeout` 值
2. 使用了 uvloop → 确认 Dockerfile CMD 包含 `--loop asyncio`
3. agent-browser 进程阻塞 → 检查容器日志

**解决方案**:
```bash
# 增大超时
POST /exec {"cmd": "open https://slow-site.com", "timeout": 120}

# 检查容器日志
docker logs <gull-container-name>
```

#### agent-browser 未找到

**症状**: `exit_code: -1`，`stderr: "agent-browser not found. Is it installed?"`

**可能原因**: Docker 镜像构建不完整，npm 安装失败

**解决方案**:
```bash
# 进入容器检查
docker exec -it <container> which agent-browser
docker exec -it <container> agent-browser --version
```

#### 健康检查返回 unhealthy

**症状**: `GET /health` 返回 `{"status": "unhealthy"}`

**可能原因**: `agent-browser` 二进制不在 PATH 中

**解决方案**:
```bash
# 检查 agent-browser 是否可用
docker exec -it <container> bash -c "which agent-browser && agent-browser --version"
```

#### Ref 失效

**症状**: `click @e1` 返回错误，提示元素不存在

**原因**: 页面导航后 ref 已失效

**解决方案**: 每次页面变化后重新执行 `snapshot -i` 获取新的 ref

```
POST /exec {"cmd": "open https://new-page.com"}
# ❌ 错误：使用旧的 ref
POST /exec {"cmd": "click @e1"}

# ✅ 正确：先重新获取快照
POST /exec {"cmd": "snapshot -i"}
POST /exec {"cmd": "click @e1"}  # 新的 @e1
```

#### 批量执行超时

**症状**: 批量执行中后续步骤超时

**原因**: 整体 timeout 预算耗尽

**解决方案**: 增大批量执行的总超时值，或减少每批的命令数量

```json
{
  "commands": ["open https://example.com", "wait --load networkidle", "snapshot -i"],
  "timeout": 120,
  "stop_on_error": true
}
```

### 9.2 调试技巧

```bash
# 查看容器日志
docker logs -f <gull-container>

# 进入容器交互调试
docker exec -it <container> bash

# 在容器内直接运行 agent-browser
agent-browser --session test open https://example.com
agent-browser --session test snapshot -i

# 检查浏览器 profile 状态
ls -la /workspace/.browser/profile/

# 检查 Chromium 是否正常
npx playwright install --dry-run chromium
```

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [Bay API v1 参考](bay_api_v1.md) | Bay 控制面 API（含浏览器接口 §2.3/2.4） |
| [Bay 错误码参考](bay_error_codes.md) | 错误码与排错指南 |
| [Bay 抽象实体](bay_abstract_entities.md) | Sandbox、Cargo 等核心概念 |
| [`pkgs/gull/README.md`](pkgs/gull/README.md) | Gull 项目简介 |
