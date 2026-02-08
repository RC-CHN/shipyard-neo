# Shipyard Neo Skills Self-Update 落地指南

本文档说明：开发者如何基于 Shipyard Neo 的现有能力，让 Agent 在运行时完成技能的采集、评估、发布与回滚。

## 1. 能力边界

Shipyard Neo 提供的是 **self-update 基建**，而不是固定训练框架：

- **运行时执行证据层**：自动记录 Python/Shell 执行历史
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

## 3. REST API 关键接口

### 3.1 Execution History

- `GET /v1/sandboxes/{sandbox_id}/history`
- `GET /v1/sandboxes/{sandbox_id}/history/last`
- `GET /v1/sandboxes/{sandbox_id}/history/{execution_id}`
- `PATCH /v1/sandboxes/{sandbox_id}/history/{execution_id}`

### 3.2 Skill Lifecycle

- `POST /v1/skills/candidates`
- `GET /v1/skills/candidates`
- `GET /v1/skills/candidates/{candidate_id}`
- `POST /v1/skills/candidates/{candidate_id}/evaluate`
- `POST /v1/skills/candidates/{candidate_id}/promote`
- `GET /v1/skills/releases`
- `POST /v1/skills/releases/{release_id}/rollback`

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
