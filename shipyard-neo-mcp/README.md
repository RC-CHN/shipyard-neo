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
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `list_files` | 列目录 |
| `delete_file` | 删除文件/目录 |
| `get_execution_history` | 查询执行历史 |
| `get_execution` | 获取单条执行记录 |
| `get_last_execution` | 获取最近执行记录 |
| `annotate_execution` | 更新执行记录注释 |
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
2. `write_file` / `execute_python` / `execute_shell`
3. `read_file`（按需）
4. `delete_sandbox`

### 2) Skills Self-Update 流程

1. 用 `execute_python` / `execute_shell` 执行任务，拿到 `execution_id`
2. 用 `annotate_execution` 标注 `description/tags/notes`
3. 用 `create_skill_candidate` 绑定一组 `source_execution_ids`
4. 用 `evaluate_skill_candidate` 记录评测结果
5. 用 `promote_skill_candidate` 发布版本（canary/stable）
6. 异常时用 `rollback_skill_release` 回滚

## 运行时防护（Guardrails）

- 参数校验：缺少必填字段或类型不合法时，返回 `**Validation Error:** ...`，不会暴露底层 `KeyError`。
- 输出截断：`execute_python` / `execute_shell` / `read_file` / 执行详情查询会统一截断超长内容，避免上下文爆炸。
- API 错误透出：`BayError` 会输出 `code + message + details(截断)`，便于上层 Agent 分支决策。
- 缓存淘汰：sandbox 缓存采用有界策略，超过 `SHIPYARD_SANDBOX_CACHE_SIZE` 后按最久未使用项淘汰。

## 关键工具参数说明

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

### `get_execution_history`

- `sandbox_id` (必填)
- `exec_type` (可选：`python` / `shell`)
- `success_only` (可选)
- `limit` (可选)
- `tags` (可选)
- `has_notes` (可选)
- `has_description` (可选)

## 许可证

AGPL-3.0-or-later
