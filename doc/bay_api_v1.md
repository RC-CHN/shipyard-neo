# Bay API v1 参考文档

> 源码位置: [`pkgs/bay/app/api/v1/`](pkgs/bay/app/api/v1/__init__.py)

## 目录

- [概览](#概览)
- [认证](#认证)
- [通用约定](#通用约定)
- [1. Sandboxes API](#1-sandboxes-api)
- [2. Capabilities API（执行能力）](#2-capabilities-api执行能力)
- [3. Cargos API（持久化存储）](#3-cargos-api持久化存储)
- [4. History API（执行历史）](#4-history-api执行历史)
- [5. Skills API（技能生命周期）](#5-skills-api技能生命周期)
- [6. Profiles API（配置档案）](#6-profiles-api配置档案)
- [7. Admin API（管理接口）](#7-admin-api管理接口)
- [错误码参考](#错误码参考)（→ [独立文档](bay_error_codes.md)）

---

## 概览

Bay API v1 是 Shipyard Neo 的控制面 REST API，基于 FastAPI 构建。它提供沙箱（Sandbox）的完整生命周期管理，包括创建、执行代码、文件操作、浏览器自动化以及技能（Skill）的演进管理。

### 路由前缀

所有 v1 端点挂载在 `/v1` 路径下，子模块的路由注册如下（见 [`__init__.py`](pkgs/bay/app/api/v1/__init__.py:13)）:

| 前缀 | 模块 | Tag | 说明 |
|------|------|-----|------|
| `/v1/sandboxes` | [`sandboxes`](pkgs/bay/app/api/v1/sandboxes.py) | sandboxes | 沙箱 CRUD 和生命周期 |
| `/v1/sandboxes` | [`capabilities`](pkgs/bay/app/api/v1/capabilities.py) | capabilities | 代码执行、文件操作、浏览器 |
| `/v1/sandboxes` | [`history`](pkgs/bay/app/api/v1/history.py) | history | 执行历史查询与标注 |
| `/v1/cargos` | [`cargos`](pkgs/bay/app/api/v1/cargos.py) | cargos | 持久化存储卷管理 |
| `/v1/skills` | [`skills`](pkgs/bay/app/api/v1/skills.py) | skills | 技能候选、评估、发布 |
| `/v1/profiles` | [`profiles`](pkgs/bay/app/api/v1/profiles.py) | profiles | Profile 配置查询 |
| `/v1/admin` | [`admin`](pkgs/bay/app/api/v1/admin.py) | admin | GC 管理等运维接口 |

---

## 认证

认证逻辑定义在 [`dependencies.authenticate()`](pkgs/bay/app/api/dependencies.py:82)。

### 认证流程

```
请求进入
  ├─ 携带 Authorization: Bearer <token>
  │   ├─ 配置了 api_key → token == api_key ? 通过 : 401
  │   └─ 未配置 api_key + allow_anonymous → 通过
  │
  └─ 未携带 token
      ├─ allow_anonymous = true → 通过（可选 X-Owner header）
      └─ allow_anonymous = false → 401
```

- **单租户模式**: 当前所有请求的 owner 固定返回 `"default"`
- **开发模式**: `allow_anonymous: true` 时，支持通过 `X-Owner` header 指定 owner（用于测试）

### 幂等性支持

部分写操作（创建沙箱、创建 Cargo、延长 TTL）支持 `Idempotency-Key` header：

```http
POST /v1/sandboxes
Idempotency-Key: my-unique-key-123
```

相同 key + 相同请求体 → 返回缓存响应，不重复执行。

---

## 通用约定

### 分页

列表接口使用游标分页（cursor-based pagination）：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 每页条数，1-200 |
| `cursor` | string \| null | null | 分页游标 |

响应中包含 `next_cursor`，为 null 时表示无更多数据。

### 路径校验

所有接受文件路径的参数均会进行安全校验（见 [`validators/path.py`](pkgs/bay/app/validators/path.py)）：
- 不允许绝对路径（以 `/` 开头）
- 不允许路径穿越（`../` 逃逸工作区）
- 不允许 null 字节

### 错误响应格式

```json
{
  "error": {
    "code": "not_found",
    "message": "Resource not found",
    "request_id": "...",
    "details": {}
  }
}
```

---

## 1. Sandboxes API

> 源码: [`sandboxes.py`](pkgs/bay/app/api/v1/sandboxes.py)
> 前缀: `/v1/sandboxes`

沙箱是 Bay 的核心资源——一个隔离的计算环境，包含容器会话（Session）和工作区存储（Cargo）。

### 1.1 创建沙箱

```
POST /v1/sandboxes
```

创建一个新的沙箱实例。采用惰性会话创建策略——沙箱创建后初始状态为 `idle`，首次执行代码时才会启动容器。

**请求体** ([`CreateSandboxRequest`](pkgs/bay/app/api/v1/sandboxes.py:25)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `profile` | string | `"python-default"` | Profile ID，决定运行时能力和资源 |
| `cargo_id` | string \| null | null | 关联已有 Cargo；null 则自动创建 |
| `ttl` | int \| null | null | 生存时间（秒），null/0 表示永不过期 |

**请求头**:

| Header | 说明 |
|--------|------|
| `Idempotency-Key` | 可选，幂等键用于安全重试 |

**响应** `201` ([`SandboxResponse`](pkgs/bay/app/api/v1/sandboxes.py:33)):

```json
{
  "id": "sbx_abc123",
  "status": "idle",
  "profile": "python-default",
  "cargo_id": "crg_xyz789",
  "capabilities": ["python", "shell", "filesystem"],
  "created_at": "2025-01-01T00:00:00Z",
  "expires_at": "2025-01-01T01:00:00Z",
  "idle_expires_at": null
}
```

**状态说明**:

| 状态 | 含义 |
|------|------|
| `idle` | 无运行会话 |
| `starting` | 会话正在启动 |
| `ready` | 会话已运行并就绪 |
| `failed` | 上一次会话启动失败 |
| `expired` | TTL 已到期 |

### 1.2 列出沙箱

```
GET /v1/sandboxes
```

列出当前用户的所有沙箱。

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 1-200 |
| `cursor` | string \| null | null | 分页游标 |
| `status` | string \| null | null | 按状态过滤: `idle`, `starting`, `ready`, `failed`, `expired` |

**响应** `200` ([`SandboxListResponse`](pkgs/bay/app/api/v1/sandboxes.py:46)):

```json
{
  "items": [{ "...SandboxResponse" }],
  "next_cursor": "cursor_token_or_null"
}
```

### 1.3 获取沙箱详情

```
GET /v1/sandboxes/{sandbox_id}
```

**响应** `200`: [`SandboxResponse`](pkgs/bay/app/api/v1/sandboxes.py:33)

### 1.4 延长 TTL

```
POST /v1/sandboxes/{sandbox_id}/extend_ttl
```

延长沙箱的过期时间（`expires_at`）。

**请求体** ([`ExtendTTLRequest`](pkgs/bay/app/api/v1/sandboxes.py:53)):

| 字段 | 类型 | 说明 |
|------|------|------|
| `extend_by` | int | 延长秒数 |

**约束**:
- 不能复活已过期的沙箱 → `409 sandbox_expired`
- 不能延长无限 TTL 的沙箱 → `409 sandbox_ttl_infinite`
- 支持 `Idempotency-Key`

**响应** `200`: [`SandboxResponse`](pkgs/bay/app/api/v1/sandboxes.py:33)（更新后的 `expires_at`）

### 1.5 保活

```
POST /v1/sandboxes/{sandbox_id}/keepalive
```

重置空闲超时计时器（`idle_expires_at`），**不** 延长 TTL。不会隐式启动计算（如果没有活跃会话）。

**响应** `200`:
```json
{ "status": "ok" }
```

### 1.6 停止沙箱

```
POST /v1/sandboxes/{sandbox_id}/stop
```

停止沙箱——回收计算资源（销毁容器），但保留工作区存储（Cargo）。幂等操作，重复调用不会报错。

**响应** `200`:
```json
{ "status": "stopped" }
```

### 1.7 删除沙箱

```
DELETE /v1/sandboxes/{sandbox_id}
```

永久删除沙箱。

**行为**:
- 销毁所有运行中的会话
- 级联删除托管 Cargo（`managed=true`）
- **不** 级联删除外部 Cargo（`managed=false`）

**响应** `204`: 无响应体

---

## 2. Capabilities API（执行能力）

> 源码: [`capabilities.py`](pkgs/bay/app/api/v1/capabilities.py)
> 前缀: `/v1/sandboxes/{sandbox_id}/...`

Capabilities API 将执行请求路由到沙箱内的运行时适配器（Ship/Gull）。每个端点都带有**能力级别校验**——在路由到运行时之前先检查 Profile 是否支持该能力。

能力校验通过 FastAPI 依赖注入实现（见 [`require_capability()`](pkgs/bay/app/api/dependencies.py:142)）：
- Level 1（Profile）: API 层检查 → `400 capability_not_supported`
- Level 2（Runtime）: 运行时容器内检查

### 2.1 Python 代码执行

```
POST /v1/sandboxes/{sandbox_id}/python/exec
```

在沙箱中执行 Python 代码。首次调用会自动启动容器会话。代码通过 IPython kernel 执行，支持富文本输出（图片等）。

**需要能力**: `python`

**请求体** ([`PythonExecRequest`](pkgs/bay/app/api/v1/capabilities.py:61)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `code` | string | *必填* | Python 代码 |
| `timeout` | int | 30 | 超时秒数，1-300 |
| `include_code` | bool | false | 响应中是否回显代码 |
| `description` | string \| null | null | 执行描述（记录到历史） |
| `tags` | string \| null | null | 标签（记录到历史） |

**响应** `200` ([`PythonExecResponse`](pkgs/bay/app/api/v1/capabilities.py:71)):

```json
{
  "success": true,
  "output": "Hello World\n",
  "error": null,
  "data": {
    "execution_count": 1,
    "output": {
      "text": "Hello World\n",
      "images": [{"image/png": "base64..."}]
    }
  },
  "execution_id": "exe_abc123",
  "execution_time_ms": 142,
  "code": null
}
```

| 字段 | 说明 |
|------|------|
| `data` | IPython 内核富输出，包含 `execution_count`、文本和图片 |
| `execution_id` | 执行历史记录 ID，可用于后续查询/标注 |
| `code` | 仅当 `include_code=true` 时返回 |

### 2.2 Shell 命令执行

```
POST /v1/sandboxes/{sandbox_id}/shell/exec
```

在沙箱中执行 Shell 命令。

**需要能力**: `shell`

**请求体** ([`ShellExecRequest`](pkgs/bay/app/api/v1/capabilities.py:93)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `command` | string | *必填* | Shell 命令 |
| `timeout` | int | 30 | 超时秒数，1-300 |
| `cwd` | string \| null | null | 工作目录（相对 `/workspace`，经过路径校验） |
| `include_code` | bool | false | 响应中是否回显命令 |
| `description` | string \| null | null | 执行描述 |
| `tags` | string \| null | null | 标签 |

**响应** `200` ([`ShellExecResponse`](pkgs/bay/app/api/v1/capabilities.py:110)):

```json
{
  "success": true,
  "output": "total 4\ndrwxr-xr-x 2 user user 4096 ...\n",
  "error": null,
  "exit_code": 0,
  "execution_id": "exe_def456",
  "execution_time_ms": 85,
  "command": null
}
```

### 2.3 浏览器命令执行

```
POST /v1/sandboxes/{sandbox_id}/browser/exec
```

在沙箱中执行浏览器自动化命令，路由到 Gull 运行时。

**需要能力**: `browser`

**请求体** ([`BrowserExecRequest`](pkgs/bay/app/api/v1/capabilities.py:168)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `cmd` | string | *必填* | 浏览器自动化命令 |
| `timeout` | int | 30 | 超时秒数，1-300 |

**响应** `200` ([`BrowserExecResponse`](pkgs/bay/app/api/v1/capabilities.py:175)):

```json
{
  "success": true,
  "output": "...",
  "error": null,
  "exit_code": 0
}
```

### 2.4 浏览器批量执行

```
POST /v1/sandboxes/{sandbox_id}/browser/exec_batch
```

批量执行多条浏览器自动化命令。整批作为单条执行历史记录。

**需要能力**: `browser`

**请求体** ([`BrowserBatchExecRequest`](pkgs/bay/app/api/v1/capabilities.py:184)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `commands` | list[string] | *必填* | 命令列表（至少 1 条） |
| `timeout` | int | 60 | 整批超时秒数，1-600 |
| `stop_on_error` | bool | true | 遇错是否停止 |

**响应** `200` ([`BrowserBatchExecResponse`](pkgs/bay/app/api/v1/capabilities.py:203)):

```json
{
  "results": [
    {
      "cmd": "navigate https://example.com",
      "stdout": "...",
      "stderr": "",
      "exit_code": 0,
      "step_index": 0,
      "duration_ms": 1200
    }
  ],
  "total_steps": 3,
  "completed_steps": 3,
  "success": true,
  "duration_ms": 3500
}
```

### 2.5 读取文件

```
GET /v1/sandboxes/{sandbox_id}/filesystem/files?path=src/main.py
```

**需要能力**: `filesystem`

**查询参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `path` | string | 文件路径（相对 `/workspace`，必填） |

**响应** `200` ([`FileReadResponse`](pkgs/bay/app/api/v1/capabilities.py:128)):

```json
{ "content": "print('hello')\n" }
```

### 2.6 写入文件

```
PUT /v1/sandboxes/{sandbox_id}/filesystem/files
```

**需要能力**: `filesystem`

**请求体** ([`FileWriteRequest`](pkgs/bay/app/api/v1/capabilities.py:134)):

| 字段 | 类型 | 说明 |
|------|------|------|
| `path` | string | 文件路径（相对 `/workspace`，经过校验） |
| `content` | string | 文件内容 |

**响应** `200`:
```json
{ "status": "ok" }
```

### 2.7 列出目录

```
GET /v1/sandboxes/{sandbox_id}/filesystem/directories?path=.
```

**需要能力**: `filesystem`

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `path` | string | `"."` | 目录路径（相对 `/workspace`） |

**响应** `200` ([`FileListResponse`](pkgs/bay/app/api/v1/capabilities.py:153)):

```json
{
  "entries": [
    { "name": "main.py", "type": "file", "size": 1024 },
    { "name": "src", "type": "directory" }
  ]
}
```

### 2.8 删除文件

```
DELETE /v1/sandboxes/{sandbox_id}/filesystem/files?path=temp.txt
```

**需要能力**: `filesystem`

**查询参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `path` | string | 要删除的路径（必填） |

**响应** `200`:
```json
{ "status": "ok" }
```

### 2.9 上传文件

```
POST /v1/sandboxes/{sandbox_id}/filesystem/upload
Content-Type: multipart/form-data
```

上传二进制文件到沙箱工作区。

**需要能力**: `filesystem`

**Form 参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `file` | file | 要上传的文件 |
| `path` | string | 目标路径（相对 `/workspace`） |

**响应** `200` ([`FileUploadResponse`](pkgs/bay/app/api/v1/capabilities.py:469)):

```json
{
  "status": "ok",
  "path": "data/input.csv",
  "size": 4096
}
```

### 2.10 下载文件

```
GET /v1/sandboxes/{sandbox_id}/filesystem/download?path=output.csv
```

以二进制流下载沙箱中的文件。

**需要能力**: `filesystem`

**查询参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `path` | string | 文件路径（必填） |

**响应** `200`: 二进制文件流
- `Content-Type: application/octet-stream`
- `Content-Disposition: attachment; filename="output.csv"`

---

## 3. Cargos API（持久化存储）

> 源码: [`cargos.py`](pkgs/bay/app/api/v1/cargos.py)
> 前缀: `/v1/cargos`

Cargo 是沙箱的持久化工作区存储卷。分为两种类型：

| 类型 | `managed` | 生命周期 | 说明 |
|------|-----------|----------|------|
| **托管 Cargo** | `true` | 随沙箱创建/删除 | 沙箱自动创建，删除沙箱时级联删除 |
| **外部 Cargo** | `false` | 用户手动管理 | 通过 API 创建，可跨多个沙箱共享 |

### 3.1 创建 Cargo

```
POST /v1/cargos
```

创建一个新的**外部** Cargo（`managed=false`）。外部 Cargo 需要用户手动删除，可在多个沙箱间共享。

**请求体** ([`CreateCargoRequest`](pkgs/bay/app/api/v1/cargos.py:28)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `size_limit_mb` | int \| null | null | 大小限制（MB），1-65536，null 使用默认值 |

**请求头**:

| Header | 说明 |
|--------|------|
| `Idempotency-Key` | 可选，幂等键 |

**响应** `201` ([`CargoResponse`](pkgs/bay/app/api/v1/cargos.py:39)):

```json
{
  "id": "crg_abc123",
  "managed": false,
  "managed_by_sandbox_id": null,
  "backend": "docker-volume",
  "size_limit_mb": 512,
  "created_at": "2025-01-01T00:00:00Z",
  "last_accessed_at": "2025-01-01T00:00:00Z"
}
```

### 3.2 列出 Cargo

```
GET /v1/cargos
```

列出当前用户的 Cargo。

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 1-200 |
| `cursor` | string \| null | null | 分页游标 |
| `managed` | bool \| null | null | 过滤托管状态。默认（null/省略）仅返回外部 Cargo；`true` 仅返回托管 Cargo |

**响应** `200` ([`CargoListResponse`](pkgs/bay/app/api/v1/cargos.py:54)):

```json
{
  "items": [{ "...CargoResponse" }],
  "next_cursor": null
}
```

### 3.3 获取 Cargo 详情

```
GET /v1/cargos/{cargo_id}
```

**响应** `200`: [`CargoResponse`](pkgs/bay/app/api/v1/cargos.py:39)

### 3.4 删除 Cargo

```
DELETE /v1/cargos/{cargo_id}
```

**删除约束**:

| 场景 | 行为 |
|------|------|
| 外部 Cargo 仍被活跃沙箱引用 | `409 conflict`，响应含 `active_sandbox_ids` |
| 托管 Cargo 的管理沙箱仍活跃 | `409 conflict` |
| 托管 Cargo 的管理沙箱已软删除 | 允许删除 |

**响应** `204`: 无响应体

---

## 4. History API（执行历史）

> 源码: [`history.py`](pkgs/bay/app/api/v1/history.py)
> 前缀: `/v1/sandboxes/{sandbox_id}/history`

执行历史在 Bay 控制面存储，按沙箱归属关系进行隔离。每次 Python/Shell/Browser 执行都会自动记录。

### 4.1 查询执行历史

```
GET /v1/sandboxes/{sandbox_id}/history
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `exec_type` | string \| null | null | 按类型过滤: `python`, `shell` |
| `success_only` | bool | false | 仅返回成功的执行 |
| `limit` | int | 100 | 1-500 |
| `offset` | int | 0 | 偏移量 |
| `tags` | string \| null | null | 按标签过滤 |
| `has_notes` | bool | false | 仅返回有备注的记录 |
| `has_description` | bool | false | 仅返回有描述的记录 |

**响应** `200` ([`ExecutionHistoryResponse`](pkgs/bay/app/api/v1/history.py:36)):

```json
{
  "entries": [
    {
      "id": "exe_abc123",
      "session_id": "ses_xyz",
      "exec_type": "python",
      "code": "print('hello')",
      "success": true,
      "execution_time_ms": 142,
      "output": "hello\n",
      "error": null,
      "description": "测试代码",
      "tags": "test",
      "notes": null,
      "created_at": "2025-01-01T00:00:00Z"
    }
  ],
  "total": 42
}
```

### 4.2 获取最近一条执行

```
GET /v1/sandboxes/{sandbox_id}/history/last
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `exec_type` | string \| null | null | 按类型过滤: `python`, `shell` |

**响应** `200`: [`ExecutionHistoryEntryResponse`](pkgs/bay/app/api/v1/history.py:19)

### 4.3 获取指定执行记录

```
GET /v1/sandboxes/{sandbox_id}/history/{execution_id}
```

**响应** `200`: [`ExecutionHistoryEntryResponse`](pkgs/bay/app/api/v1/history.py:19)

### 4.4 标注执行记录

```
PATCH /v1/sandboxes/{sandbox_id}/history/{execution_id}
```

为执行记录添加描述、标签或备注。

**请求体** ([`AnnotateExecutionRequest`](pkgs/bay/app/api/v1/history.py:43)):

| 字段 | 类型 | 说明 |
|------|------|------|
| `description` | string \| null | 执行描述 |
| `tags` | string \| null | 标签 |
| `notes` | string \| null | 备注 |

所有字段均为可选，仅更新提供的字段。

**响应** `200`: [`ExecutionHistoryEntryResponse`](pkgs/bay/app/api/v1/history.py:19)（更新后的记录）

---

## 5. Skills API（技能生命周期）

> 源码: [`skills.py`](pkgs/bay/app/api/v1/skills.py)
> 前缀: `/v1/skills`

Skills API 管理技能的完整生命周期：候选创建 → 评估 → 晋升发布 → 回滚。技能（Skill）是由执行历史提炼而成的可复用代码片段。

### 生命周期流程

```
执行历史（Executions）
    ↓ create_candidate（选取有价值的执行）
Candidate（候选）
    ↓ evaluate（评估打分）
Candidate（已评估，passed=true）
    ↓ promote（晋升到 canary/stable）
Release（发布版本）
    ↓ rollback（如有问题）
Release（回滚版本）
```

### 5.1 通用 Payload（新增）

Skills 域提供通用 payload 读写能力，推荐用于 candidate payload 与其他可复用工件内容存储。  
写入后返回 `blob:` 引用（`payload_ref`），后续通过该引用读取。

#### 5.1.1 创建 payload

```
POST /v1/skills/payloads
```

**请求体**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `payload` | object \| array | *必填* | JSON 负载内容（仅支持 object/array） |
| `kind` | string | `"generic"` | 负载类型标记 |

**响应** `201`:

```json
{
  "payload_ref": "blob:blob_abc123",
  "kind": "candidate_payload"
}
```

#### 5.1.2 读取 payload

```
GET /v1/skills/payloads/{payload_ref}
```

**响应** `200`:

```json
{
  "payload_ref": "blob:blob_abc123",
  "kind": "candidate_payload",
  "payload": {
    "commands": ["open about:blank"]
  }
}
```

**错误语义**:

| 场景 | 状态码 | 说明 |
|------|--------|------|
| `payload_ref` 非 `blob:` 格式 | `400 validation_error` | 不支持的引用类型 |
| `blob:` 引用不存在或不可见 | `404 not_found` | 资源未找到 |

> 兼容说明：`GET /v1/sandboxes/{sandbox_id}/browser/traces/{trace_ref}` 仍保留，响应结构保持不变；其内部读取逻辑与通用 payload 存储一致。

### 5.2 创建候选

```
POST /v1/skills/candidates
```

**请求体** ([`SkillCandidateCreateRequest`](pkgs/bay/app/api/v1/skills.py:17)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `skill_key` | string | *必填* | 技能标识键 |
| `source_execution_ids` | list[string] | *必填* | 来源执行记录 ID 列表 |
| `scenario_key` | string \| null | null | 场景标识 |
| `payload_ref` | string \| null | null | 负载引用 |

**响应** `201` ([`SkillCandidateResponse`](pkgs/bay/app/api/v1/skills.py:26)):

```json
{
  "id": "cand_abc123",
  "skill_key": "data-cleaning-v1",
  "scenario_key": null,
  "payload_ref": null,
  "source_execution_ids": ["exe_001", "exe_002"],
  "status": "draft",
  "latest_score": null,
  "latest_pass": null,
  "last_evaluated_at": null,
  "promotion_release_id": null,
  "created_by": "default",
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:00:00Z"
}
```

**候选状态**:

| 状态 | 说明 |
|------|------|
| `draft` | 草稿，待评估 |
| `evaluating` | 评估中 |
| `promoted` | 已晋升为发布版本 |
| `rejected` | 已拒绝 |
| `rolled_back` | 已回滚 |

### 5.3 列出候选

```
GET /v1/skills/candidates
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `status` | string \| null | null | 按状态过滤 |
| `skill_key` | string \| null | null | 按技能键过滤 |
| `limit` | int | 100 | 1-500 |
| `offset` | int | 0 | 偏移量 |

**响应** `200` ([`SkillCandidateListResponse`](pkgs/bay/app/api/v1/skills.py:44)):

```json
{
  "items": [{ "...SkillCandidateResponse" }],
  "total": 10
}
```

### 5.4 获取候选详情

```
GET /v1/skills/candidates/{candidate_id}
```

**响应** `200`: [`SkillCandidateResponse`](pkgs/bay/app/api/v1/skills.py:26)

### 5.5 评估候选

```
POST /v1/skills/candidates/{candidate_id}/evaluate
```

**请求体** ([`SkillEvaluationRequest`](pkgs/bay/app/api/v1/skills.py:51)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `passed` | bool | *必填* | 是否通过 |
| `score` | float \| null | null | 评分 |
| `benchmark_id` | string \| null | null | 基准测试 ID |
| `report` | string \| null | null | 评估报告 |

**响应** `200` ([`SkillEvaluationResponse`](pkgs/bay/app/api/v1/skills.py:60)):

```json
{
  "id": "eval_xyz",
  "candidate_id": "cand_abc123",
  "benchmark_id": null,
  "score": 0.95,
  "passed": true,
  "report": "All tests passed",
  "evaluated_by": "default",
  "created_at": "2025-01-01T00:05:00Z"
}
```

### 5.6 晋升候选

```
POST /v1/skills/candidates/{candidate_id}/promote
```

将已通过评估的候选晋升为发布版本。

**请求体** ([`SkillPromotionRequest`](pkgs/bay/app/api/v1/skills.py:73)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `stage` | string | `"canary"` | 发布阶段: `canary`, `stable` |

**响应** `200` ([`SkillReleaseResponse`](pkgs/bay/app/api/v1/skills.py:79)):

```json
{
  "id": "rel_001",
  "skill_key": "data-cleaning-v1",
  "candidate_id": "cand_abc123",
  "version": 1,
  "stage": "canary",
  "is_active": true,
  "promoted_by": "default",
  "promoted_at": "2025-01-01T00:10:00Z",
  "rollback_of": null
}
```

### 5.7 列出发布版本

```
GET /v1/skills/releases
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `skill_key` | string \| null | null | 按技能键过滤 |
| `active_only` | bool | false | 仅返回活跃版本 |
| `stage` | string \| null | null | 按阶段过滤: `canary`, `stable` |
| `limit` | int | 100 | 1-500 |
| `offset` | int | 0 | 偏移量 |

**响应** `200` ([`SkillReleaseListResponse`](pkgs/bay/app/api/v1/skills.py:93)):

```json
{
  "items": [{ "...SkillReleaseResponse" }],
  "total": 5
}
```

### 5.8 回滚发布

```
POST /v1/skills/releases/{release_id}/rollback
```

回滚指定的发布版本。

**响应** `200`: [`SkillReleaseResponse`](pkgs/bay/app/api/v1/skills.py:79)（新创建的回滚版本，`rollback_of` 指向被回滚的版本 ID）

---

## 6. Profiles API（配置档案）

> 源码: [`profiles.py`](pkgs/bay/app/api/v1/profiles.py)
> 前缀: `/v1/profiles`

Profile 定义了沙箱的运行时配置模板，包括镜像、资源限制、能力集合和空闲超时。Profile 在服务端配置文件中定义，API 仅提供只读查询。

### 6.1 列出 Profiles

```
GET /v1/profiles
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `detail` | bool | false | 是否包含容器拓扑和描述信息 |

**响应** `200` ([`ProfileListResponse`](pkgs/bay/app/api/v1/profiles.py:44)):

```json
{
  "items": [
    {
      "id": "python-default",
      "image": "shipyard/ship:latest",
      "resources": { "cpus": 1.0, "memory": "1g" },
      "capabilities": ["python", "shell", "filesystem"],
      "idle_timeout": 600,
      "description": null,
      "containers": null
    }
  ]
}
```

**`detail=true` 时额外返回**:

| 字段 | 说明 |
|------|------|
| `description` | Profile 描述文本 |
| `containers` | 容器拓扑列表（多容器 Profile 支持） |

```json
{
  "containers": [
    {
      "name": "ship",
      "runtime_type": "ship",
      "capabilities": ["filesystem", "python", "shell"],
      "resources": { "cpus": 1.0, "memory": "1g" }
    },
    {
      "name": "gull",
      "runtime_type": "gull",
      "capabilities": ["browser"],
      "resources": { "cpus": 0.5, "memory": "512m" }
    }
  ]
}
```

---

## 7. Admin API（管理接口）

> 源码: [`admin.py`](pkgs/bay/app/api/v1/admin.py)
> 前缀: `/v1/admin`

运维管理接口，目前提供 GC（垃圾回收）的手动触发和状态查询。

### 7.1 手动触发 GC

```
POST /v1/admin/gc/run
```

同步执行一次 GC 周期，等待完成后返回详细结果。即使 `gc.enabled: false`（后台 GC 关闭）时仍可使用。

**请求体**（可选）([`GCRunRequest`](pkgs/bay/app/api/v1/admin.py:26)):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tasks` | list[string] \| null | null | 指定运行的任务列表，null 表示全部 |

**可用任务名**:

| 任务名 | 说明 |
|--------|------|
| `idle_session` | 回收空闲超时的会话 |
| `expired_sandbox` | 清理 TTL 过期的沙箱 |
| `orphan_cargo` | 清理孤儿 Cargo |
| `orphan_container` | 清理孤儿容器 |

**响应** `200` ([`GCRunResponse`](pkgs/bay/app/api/v1/admin.py:45)):

```json
{
  "results": [
    {
      "task_name": "idle_session",
      "cleaned_count": 3,
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
  "total_cleaned": 4,
  "total_errors": 0,
  "duration_ms": 523
}
```

**错误状态码**:

| 状态码 | 说明 |
|--------|------|
| `200` | GC 执行完成（即使部分任务有错误） |
| `423` | GC 已在运行中（另一个周期进行中） |
| `503` | GC 调度器不可用（内部异常） |

### 7.2 查询 GC 状态

```
GET /v1/admin/gc/status
```

获取 GC 调度器当前的配置和运行状态。

**响应** `200` ([`GCStatusResponse`](pkgs/bay/app/api/v1/admin.py:54)):

```json
{
  "enabled": true,
  "is_running": false,
  "instance_id": "bay-gc-01",
  "interval_seconds": 60,
  "tasks": {
    "idle_session": { "enabled": true },
    "expired_sandbox": { "enabled": true },
    "orphan_cargo": { "enabled": true },
    "orphan_container": { "enabled": true }
  }
}
```

---

## 错误码参考

→ 详见独立文档: [Bay 错误码参考](bay_error_codes.md)
