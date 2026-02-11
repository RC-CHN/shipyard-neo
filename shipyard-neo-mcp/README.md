# Shipyard Neo MCP Server

Shipyard Neo 的 MCP (Model Context Protocol) 接入层。  
让 Agent 通过 MCP 工具直接调用 Bay 沙箱能力与 skills self-update 能力。

## 工具总览

| 工具 | 描述 |
|:--|:--|
| `create_sandbox` | 创建沙箱 |
| `delete_sandbox` | 删除沙箱 |
| `execute_python` | 执行 Python（支持 `include_code/description/tags`） |
| `execute_shell` | 执行 Shell（支持 `include_code/description/tags`） |
| `read_file` | 读取文件（文本，sandbox 内） |
| `write_file` | 写入文件（文本，sandbox 内） |
| `upload_file` | 上传本地文件到 sandbox（支持二进制） |
| `download_file` | 从 sandbox 下载文件到本地（支持二进制） |
| `list_files` | 列目录 |
| `delete_file` | 删除文件/目录 |
| `get_execution_history` | 查询执行历史 |
| `get_execution` | 获取单条执行记录 |
| `get_last_execution` | 获取最近执行记录 |
| `annotate_execution` | 更新执行记录注释 |
| `create_skill_payload` | 创建通用技能 payload，返回 `payload_ref` |
| `get_skill_payload` | 通过 `payload_ref` 读取技能 payload |
| `create_skill_candidate` | 创建技能候选 |
| `evaluate_skill_candidate` | 记录候选评测结果 |
| `promote_skill_candidate` | 发布候选为版本 |
| `list_skill_candidates` | 查询候选列表 |
| `list_skill_releases` | 查询发布列表 |
| `rollback_skill_release` | 回滚发布版本 |

## 安装

```bash
pip install shipyard-neo-mcp
```

或源码安装：

```bash
cd shipyard-neo-mcp
pip install -e .
```

## 配置

### 环境变量

优先读取 `SHIPYARD_*`，若未设置会回退到 `BAY_*`（仅 endpoint/token）。

| 变量 | 描述 | 必需 |
|:--|:--|:--|
| `SHIPYARD_ENDPOINT_URL` | Bay API 地址 | ✅（或 `BAY_ENDPOINT`） |
| `SHIPYARD_ACCESS_TOKEN` | 访问令牌 | ✅（或 `BAY_TOKEN`） |
| `SHIPYARD_DEFAULT_PROFILE` | 默认 profile（默认 `python-default`） | ❌ |
| `SHIPYARD_DEFAULT_TTL` | 默认 TTL 秒数（默认 `3600`） | ❌ |
| `SHIPYARD_MAX_TOOL_TEXT_CHARS` | 工具返回文本截断上限（默认 `12000`） | ❌ |
| `SHIPYARD_SANDBOX_CACHE_SIZE` | sandbox 本地缓存上限（默认 `256`） | ❌ |
| `SHIPYARD_MAX_WRITE_FILE_BYTES` | `write_file` 写入内容大小上限（默认 `5242880` = 5MB） | ❌ |
| `SHIPYARD_MAX_TRANSFER_FILE_BYTES` | `upload_file`/`download_file` 文件大小上限（默认 `52428800` = 50MB） | ❌ |
| `SHIPYARD_SDK_CALL_TIMEOUT` | SDK 调用全局超时秒数（默认 `600`） | ❌ |

### MCP 配置示例

```json
{
  "mcpServers": {
    "shipyard-neo": {
      "command": "shipyard-mcp",
      "env": {
        "SHIPYARD_ENDPOINT_URL": "http://localhost:8000",
        "SHIPYARD_ACCESS_TOKEN": "your-access-token"
      }
    }
  }
}
```

或使用 Python 模块启动：

```json
{
  "mcpServers": {
    "shipyard-neo": {
      "command": "python",
      "args": ["-m", "shipyard_neo_mcp"],
      "env": {
        "SHIPYARD_ENDPOINT_URL": "http://localhost:8000",
        "SHIPYARD_ACCESS_TOKEN": "your-access-token"
      }
    }
  }
}
```

## 常用流程

### 1) 基础执行流程

1. `create_sandbox`
2. `write_file` / `upload_file` / `execute_python` / `execute_shell`
3. `read_file` / `download_file`（按需）
4. `delete_sandbox`

### 1.5) 本地文件传输流程

1. `create_sandbox`
2. `upload_file` — 将本地文件（二进制/文本）上传到 sandbox（例如数据集、图片等）
3. `execute_python` / `execute_shell` — 在 sandbox 中处理文件
4. `download_file` — 将处理结果下载到本地
5. `delete_sandbox`

### 2) Skills Self-Update 流程

1. 用 `execute_python` / `execute_shell` 执行任务，拿到 `execution_id`
2. 用 `annotate_execution` 标注 `description/tags/notes`
3. 可选：用 `create_skill_payload` 存储候选 payload，拿到 `payload_ref`
4. 用 `create_skill_candidate` 绑定 `source_execution_ids`（可附带 `payload_ref`）
5. 用 `evaluate_skill_candidate` 记录评测结果
6. 用 `promote_skill_candidate` 发布版本（canary/stable）
7. 异常时用 `rollback_skill_release` 回滚

## 运行时防护（Guardrails）

### 参数校验

- 缺少必填字段或类型不合法时，返回 `**Validation Error:** ...`，不会暴露底层 `KeyError`。
- 所有 `sandbox_id` 经过正则格式校验（`^[a-zA-Z0-9_-]{1,128}$`），拒绝路径穿越等注入攻击。
- 枚举值（`exec_type`、`stage`）和数值范围（`limit`、`timeout`）有白名单/边界检查（`exec_type` 支持 `python/shell/browser/browser_batch`）。

### 输出截断

- `execute_python` / `execute_shell` / `read_file` / 执行详情查询会统一截断超长内容，避免上下文爆炸。
- 截断上限由 `SHIPYARD_MAX_TOOL_TEXT_CHARS` 控制（默认 12000 字符）。
- 截断后追加 `...[truncated N chars; original=M]` 标记，保留可观测性。

### 写入大小限制

- `write_file` 会检查内容 UTF-8 编码后的字节大小。
- 超过 `SHIPYARD_MAX_WRITE_FILE_BYTES`（默认 5MB）时返回校验错误，防止 Agent 意外写入过大文件。

### 文件传输大小限制

- `upload_file` 和 `download_file` 会检查文件字节大小。
- 超过 `SHIPYARD_MAX_TRANSFER_FILE_BYTES`（默认 50MB）时返回校验错误。
- `upload_file` 会验证本地文件存在、是否为常规文件。
- `download_file` 会自动创建本地目标路径的父目录。

### SDK 调用超时

- `create_sandbox` / `delete_sandbox` / `get_sandbox` 等底层 SDK 调用统一包裹 `asyncio.timeout`。
- 超时上限由 `SHIPYARD_SDK_CALL_TIMEOUT` 控制（默认 600 秒）。
- 超时后返回 `**Timeout Error:** SDK call timed out after Ns`，防止无限阻塞。

### API 错误透出

- `BayError` 会输出 `code + message + details(截断)`，便于上层 Agent 分支决策。
- `details` 截断到 1000 字符，避免过大错误详情占据上下文。

### 并发安全 & 缓存淘汰

- sandbox 对象缓存使用 `asyncio.Lock` 保护读写操作，防止并发竞态条件。
- 缓存采用有界 LRU 策略（`OrderedDict`），超过 `SHIPYARD_SANDBOX_CACHE_SIZE`（默认 256）后按最久未使用项淘汰。
- 淘汰事件写入 DEBUG 日志。

### 结构化日志

- 关键操作（`sandbox_created`、`sandbox_deleted`）写入 INFO 日志。
- 异常（`bay_error`、`tool_timeout`、`unexpected_error`）写入 WARNING/ERROR 日志。
- 缓存淘汰（`cache_evict`）写入 DEBUG 日志。
- 使用标准 `logging` 模块，logger name = `shipyard_neo_mcp`。

## 关键工具参数说明

### `upload_file`

- `sandbox_id` (必填)
- `local_path` (必填，本地文件路径，绝对或相对路径)
- `sandbox_path` (可选，sandbox 中的目标路径，默认使用本地文件名)

### `download_file`

- `sandbox_id` (必填)
- `sandbox_path` (必填，sandbox 中的文件路径)
- `local_path` (可选，本地保存路径，默认保存到当前目录)

### `execute_python`

- `sandbox_id` (必填)
- `code` (必填)
- `timeout` (可选，默认 30)
- `include_code` (可选，返回中附带代码)
- `description` (可选，写入执行历史)
- `tags` (可选，逗号分隔标签)

### `execute_shell`

- `sandbox_id` (必填)
- `command` (必填)
- `cwd` (可选)
- `timeout` (可选，默认 30)
- `include_code` (可选)
- `description` (可选)
- `tags` (可选)

### `create_skill_payload`

- `payload` (必填，JSON object/array)
- `kind` (可选，默认 `generic`)

### `get_skill_payload`

- `payload_ref` (必填，示例：`blob:blob-xxx`)

### `get_execution_history`

- `sandbox_id` (必填)
- `exec_type` (可选：`python` / `shell` / `browser` / `browser_batch`)
- `success_only` (可选)
- `limit` (可选)
- `tags` (可选)
- `has_notes` (可选)
- `has_description` (可选)

## 许可证

AGPL-3.0-or-later
