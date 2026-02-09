# Bay

Bay 是 Shipyard Neo 的控制面（Control Plane），负责：

- Sandbox / Session / Cargo 生命周期管理
- Profile 能力校验与路由
- Driver 抽象（Docker / Kubernetes）
- 幂等、鉴权、GC 等平台能力
- 执行历史与技能生命周期（self-update 基建）

## 核心概念

- **Sandbox**: 对外唯一资源，聚合 Cargo + Profile + Session
- **Cargo**: 持久化数据层（Docker Volume 或 K8s PVC）
- **Session**: 运行实例（Docker Container 或 K8s Pod），可回收/重建
- **Profile**: 运行时规格（镜像/资源/capabilities）
- **Execution History**: Python/Shell 执行证据记录（可注释）
- **Skill Lifecycle**: Candidate -> Evaluation -> Release -> Rollback

## 能力总览

### 1) 执行能力（Capabilities）

`python/shell` 执行在返回结果时附带：

- `execution_id`
- `execution_time_ms`
- 可选回显 `code/command`（`include_code=true`）

并支持写入语义标签：

- `description`
- `tags`

### 2) 执行历史（Execution History API）

挂载在 `/v1/sandboxes/{sandbox_id}/history`：

- `GET /history`: 列表查询（支持 `exec_type/success_only/tags/has_notes/has_description`）
- `GET /history/last`: 获取最近一次执行
- `GET /history/{execution_id}`: 获取单条记录
- `PATCH /history/{execution_id}`: 更新 `description/tags/notes`

### 3) 技能生命周期（Skill Lifecycle API）

挂载在 `/v1/skills`：

- `POST /candidates`: 创建技能候选（绑定 source execution IDs）
- `GET /candidates`: 候选列表过滤
- `GET /candidates/{candidate_id}`: 候选详情
- `POST /candidates/{candidate_id}/evaluate`: 记录评测结果
- `POST /candidates/{candidate_id}/promote`: 发布为版本（`canary/stable`）
- `GET /releases`: 发布列表（支持 `active_only/stage`）
- `POST /releases/{release_id}/rollback`: 回滚到上一个版本

## 运行与开发

```bash
# 安装依赖
uv sync

# 运行开发服务器
uv run python -m app.main

# 运行单元测试
uv run pytest tests/unit -v

# 运行集成测试（需 Bay + ship:latest + Docker/K8s 环境）
uv run pytest tests/integration -q
```

## 设计文档

- [Bay 架构设计](../../plans/bay-design.md)
- [API 契约](../../plans/bay-api.md)
- [概念与职责边界](../../plans/bay-concepts.md)
- [实现路径](../../plans/bay-implementation-path.md)
- [Phase 1 进度追踪](../../plans/phase-1/progress.md)
- [Phase 2 规划](../../plans/phase-2/phase-2.md)
- [K8s Driver 分析](../../plans/phase-2/k8s-driver-analysis.md)
