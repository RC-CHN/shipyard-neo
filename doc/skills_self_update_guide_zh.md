# Shipyard Neo Skills Self-Update 落地指南

本文档说明：开发者如何基于 Shipyard Neo 的现有能力，让 Agent 在运行时完成技能的采集、评估、发布与回滚。

## 1. 能力边界

Shipyard Neo 提供的是 **self-update 基建**，而不是固定训练框架：

- **运行时执行证据层**：自动记录 Python/Shell 执行历史
- **浏览器证据层**：browser exec/exec_batch 记录 `execution_id`、`trace_ref`、`learn` 状态
- **技能控制面**：Candidate -> Evaluation -> Release -> Rollback
- **多入口**：REST API / Python SDK / MCP tools

是否在线学习、离线评估、A/B 发布策略，由上层 Agent 系统自定义。

## 2. 端到端数据流

1. Agent 在 sandbox 中执行任务（`python/exec` 或 `shell/exec`）
2. Bay 生成并返回 `execution_id`
3. Agent 通过 history API 查询并补充 `description/tags/notes`
4. Agent 选择一组 `source_execution_ids` 创建 skill candidate
5. 评测系统写入 evaluate 结果（score/pass/report）
6. 满足条件后 promote，生成版本化 release（canary/stable）
7. 线上异常时可 rollback 到上一版本

### 2.1 Browser 自迭代闭环（自动发布）

1. Agent 调用 `POST /v1/sandboxes/{sandbox_id}/browser/exec` 或 `exec_batch`，传入 `learn=true`。
2. 可选传入 `include_trace=true`，Bay 返回 `trace_ref` 并把 step 轨迹外置到 `payload_ref=blob:<id>`。
3. Bay 后台异步任务扫描 `learn=true` 的 browser 证据，抽取连续可执行动作段（长度>=2，排除失败/只读动作）。
4. 自动创建 browser candidate 并写入回放评测结果。
5. 达到阈值时自动发 canary：`score>=0.85`、`replay_success>=95%`、`samples>=30`。
6. canary 健康窗口 24 小时达标后自动升 stable。
7. 若 `success_rate` 下降超过 3% 或 `error_rate` 升至 2x 以上，自动回滚并写审计日志。

## 3. REST API 关键接口

### 3.1 Execution History

- `GET /v1/sandboxes/{sandbox_id}/history`
- `GET /v1/sandboxes/{sandbox_id}/history/last`
- `GET /v1/sandboxes/{sandbox_id}/history/{execution_id}`
- `PATCH /v1/sandboxes/{sandbox_id}/history/{execution_id}`

### 3.2 Skill Lifecycle

- `POST /v1/skills/payloads`（推荐：先创建通用 payload，获取 `payload_ref`）
- `GET /v1/skills/payloads/{payload_ref}`
- `POST /v1/skills/candidates`
- `GET /v1/skills/candidates`
- `GET /v1/skills/candidates/{candidate_id}`
- `POST /v1/skills/candidates/{candidate_id}/evaluate`
- `POST /v1/skills/candidates/{candidate_id}/promote`
- `GET /v1/skills/releases`
- `POST /v1/skills/releases/{release_id}/rollback`
- `GET /v1/skills/releases/{release_id}/health`

### 3.3 Browser Skill APIs

- `POST /v1/sandboxes/{sandbox_id}/browser/exec`
- `POST /v1/sandboxes/{sandbox_id}/browser/exec_batch`
- `POST /v1/sandboxes/{sandbox_id}/browser/skills/{skill_key}/run`
- `GET /v1/sandboxes/{sandbox_id}/browser/traces/{trace_ref}`（兼容入口；推荐优先使用 `GET /v1/skills/payloads/{payload_ref}`）

## 4. Python SDK 示例

```python
from shipyard_neo import BayClient, SkillReleaseStage

async with BayClient(endpoint_url="http://localhost:8000", access_token="token") as client:
    sandbox = await client.create_sandbox(ttl=600)

    r1 = await sandbox.python.exec("print('step1')", tags="etl")
    r2 = await sandbox.shell.exec("echo step2", tags="etl")

    candidate = await client.skills.create_candidate(
        skill_key="etl-loader",
        source_execution_ids=[r1.execution_id, r2.execution_id],
        scenario_key="csv-import",
        payload_ref=(await client.skills.create_payload(
            payload={"commands": ["open about:blank"]},
            kind="candidate_payload",
        )).payload_ref,
    )

    await client.skills.evaluate_candidate(
        candidate.id,
        passed=True,
        score=0.95,
        benchmark_id="bench-etl-001",
        report="pass",
    )

    release = await client.skills.promote_candidate(
        candidate.id,
        stage=SkillReleaseStage.CANARY,
    )

    # 线上回滚
    await client.skills.rollback_release(release.id)
```

## 5. MCP 入口（Agent 集成）

若 Agent 通过 MCP 集成，可直接调用：

- `get_execution_history`
- `annotate_execution`
- `create_skill_payload`
- `get_skill_payload`
- `create_skill_candidate`
- `evaluate_skill_candidate`
- `promote_skill_candidate`
- `list_skill_releases`
- `rollback_skill_release`

适合“无代码 SDK 集成”场景。

## 6. 推荐实践

1. **标签规范化**：统一 tags 词表（如 `etl`, `planner`, `retrieval`, `stable`）。
2. **评测前置**：禁止直接 promote 未通过评测的 candidate。
3. **发布分级**：先 canary，再 stable。
4. **回滚自动化**：将关键线上指标绑定 rollback 触发器。
5. **证据可追溯**：candidate 必须保留 source execution IDs。
6. **渐进上线**：生产首轮建议设置 `BAY_BROWSER_AUTO_RELEASE_ENABLED=false`，验证指标稳定后再打开。

## 7. 运行期稳态保障（已内置）

1. **SDK 重试策略**：`GET/PUT/DELETE` 自动重试，`POST` 仅在带 `idempotency_key` 时重试。
2. **错误语义保留**：即使上游返回非 JSON 错误页，也会按 HTTP 状态码映射语义化异常（如 `NotFoundError`）。
3. **MCP 参数校验**：缺少必填参数会返回可读的 `Validation Error`，而不是裸 `KeyError`。
4. **MCP 输出截断**：超长工具输出自动截断并标记，避免上下文爆炸。
5. **缓存有界化**：sandbox 缓存有上限并按 LRU 淘汰，避免长时运行内存线性增长。

## 8. 测试矩阵与回归命令

建议按“单测先行，E2E 兜底”执行：

1. Bay 单测（browser learning/service 分支覆盖）
```bash
cd pkgs/bay
uv run pytest -q \
  tests/unit/managers/test_browser_learning_lifecycle.py \
  tests/unit/managers/test_skill_lifecycle_service.py \
  tests/unit/managers/test_browser_learning_scheduler.py \
  tests/unit/api/test_capabilities_browser_payloads.py
```

2. SDK 单测（browser 参数透传与 health 解析）
```bash
cd shipyard-neo-sdk
uv run pytest -q tests/test_client.py tests/test_skills_and_history.py
```

3. MCP 单测（tool schema + handler 输出）
```bash
cd shipyard-neo-sdk
PYTHONPATH=../shipyard-neo-mcp/src uv run --with mcp pytest -q ../shipyard-neo-mcp/tests/test_server.py
```

4. Bay E2E（需 Bay/Ship/Gull 环境）
```bash
cd pkgs/bay
uv run pytest -q \
  tests/integration/core/test_history_api.py \
  tests/integration/core/test_skill_lifecycle_api.py \
  tests/integration/core/test_browser_skill_e2e.py
```

`test_browser_skill_e2e.py` 会自动检查 browser profile 与 Docker 下 `gull:latest`，环境不满足时会 `skip`，不会污染单测结论。
其中包含 `run_skill` 的负例分支（无 active release、非法 payload_ref）回归。
