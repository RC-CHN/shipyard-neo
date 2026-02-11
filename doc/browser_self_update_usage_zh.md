# 浏览器 Self-Update 功能使用文档

本文档是 Shipyard Neo 浏览器 Self-Update（自更新/自进化）功能的详细操作与使用手册。旨在帮助开发者和运维人员配置、使用及管理这一自动化持续进化机制。

## 1. 快速开始

### 1.1 前置条件

*   **Bay 服务已启动**：确保 Bay 服务（API Server）正常运行且版本支持 Self-Update。
*   **Gull 运行时就绪**：确保配置了包含 `browser` 能力的 Profile（如 `browser-python` 或默认支持 browser 的配置）。
*   **数据库连接正常**：Self-Update 强依赖数据库存储执行历史和技能数据。

### 1.2 最小配置示例

在开发环境中，只需启用 Browser Learning 模块即可开始体验。在 `config.yaml` 或环境变量中设置：

```yaml
# config.yaml
browser_learning:
  enabled: true
  interval_seconds: 60  # 开发环境可缩短检测间隔便于调试
  score_threshold: 0.7  # 降低门槛以便更容易触发
```

或者使用环境变量：

```bash
export BAY_BROWSER_LEARNING__ENABLED=true
export BAY_BROWSER_LEARNING__INTERVAL_SECONDS=60
export BAY_BROWSER_LEARNING__SCORE_THRESHOLD=0.7
```

### 1.3 启用验证

1.  **发送学习请求**：
    向 Sandbox 发送一个带有 `learn=true` 的浏览器执行请求。

    ```bash
    curl -X POST "http://localhost:8000/v1/sandboxes/{id}/browser/exec" \
      -H "Content-Type: application/json" \
      -d '{
        "cmd": "open https://example.com",
        "learn": true,
        "include_trace": true,
        "tags": "demo:test"
      }'
    ```

2.  **验证学习触发**：
    观察 Bay 的日志，或者稍后查询技能候选列表。

    ```bash
    # 查询新生成的候选技能
    curl "http://localhost:8000/v1/skills/candidates"
    ```

    如果返回了新的候选条目，说明 Self-Update 流程已成功打通。

---

## 2. 配置指南

Self-Update 的核心行为主要由 `config.yaml` 中的 `browser_learning` 节或以 `BAY_BROWSER_LEARNING__` 开头的环境变量控制。

### 2.1 环境变量配置表

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `BAY_BROWSER_LEARNING__ENABLED` | `true` | 全局开关，控制是否启动后台学习调度器。 |
| `BAY_BROWSER_LEARNING__INTERVAL_SECONDS` | `300` | 学习循环的轮询间隔（秒）。 |
| `BAY_BROWSER_LEARNING__BATCH_SIZE` | `20` | 每次循环处理的 Pending 历史记录数量。 |
| `BAY_BROWSER_LEARNING__SCORE_THRESHOLD` | `0.85` | 自动提取技能的最低评分阈值（0-1）。 |
| `BAY_BROWSER_LEARNING__REPLAY_SUCCESS_THRESHOLD` | `0.95` | 自动验证回放的成功率要求。 |
| `BAY_BROWSER_LEARNING__MIN_SAMPLES` | `30` | 触发自动发布所需的最小样本数（通常用于统计显著性）。 |
| `BAY_BROWSER_LEARNING__CANARY_WINDOW_HOURS` | `24` | Canary 版本晋升 Stable 前的观察窗口期（小时）。 |
| `BAY_BROWSER_AUTO_RELEASE_ENABLED` | `true` | 是否允许系统自动将合格候选发布为 Canary 版本。 |

### 2.2 关键参数调优建议

*   **`interval_seconds`**
    *   **开发/调试**：建议设为 `10` - `60` 秒，以便快速获得反馈。
    *   **生产环境**：建议保持默认 `300` 秒或更长，避免频繁查询数据库影响性能。

*   **`score_threshold`**
    *   系统评分公式与操作步数正相关（步数越多分数越高）。
    *   默认 `0.85` 约对应 **2 个有效操作步骤**。
    *   如果希望捕捉更细粒度的原子操作，可适当降低至 `0.80`。

*   **`batch_size`**
    *   如果系统并发量大，产生大量 browser exec 记录，建议适当调大此值（如 `50` 或 `100`），避免处理积压。

### 2.3 生产环境推荐配置

在生产环境中，建议采取**保守策略**，先关闭自动发布，人工介入审核，稳定后再开启。

```yaml
# 生产环境推荐
browser_learning:
  enabled: true
  interval_seconds: 300
  score_threshold: 0.85
  # 延长 Canary 观察期，确保充分暴露问题
  canary_window_hours: 48
  
# 建议初期关闭自动发布，改为人工 Promote
browser_auto_release_enabled: false 
```

---

## 3. API 使用手册

### 3.1 触发学习

要让系统从某次操作中学习，必须在请求中显式标记 `learn=true`。建议配合 `tags` 使用，以便后续分类和检索。

**请求示例**：

```json
POST /v1/sandboxes/{id}/browser/exec
{
  "cmd": "click '#login-btn'",
  "learn": true,
  "include_trace": true,
  "tags": "category:auth,action:login"
}
```

> **注意**：只有成功的执行（`success: true`）且包含有效变更操作（非只读）的记录才会被提取。

### 3.2 技能管理

#### 查询候选技能

获取当前系统挖掘出的潜在技能列表。

```bash
GET /v1/skills/candidates?status=PENDING
```

#### 评估技能 (Evaluate)

在将技能投入使用前，可以对其进行评估打分或写入测试报告。

```json
POST /v1/skills/candidates/{candidate_id}/evaluate
{
  "passed": true,
  "score": 0.95,
  "report": "经过人工复核，逻辑正确",
  "metadata": {
    "reviewer": "admin"
  }
}
```

#### 发布技能 (Promote)

将候选技能正式发布为版本。

```json
POST /v1/skills/candidates/{candidate_id}/promote
{
  "stage": "CANARY",
  "version_tag": "v1.0.0"
}
```

### 3.3 执行技能

已发布的技能可以通过 `skill_key` 直接调用回放。

```json
POST /v1/sandboxes/{id}/browser/skills/{skill_key}/run
{
  "input_params": {}
}
```

### 3.4 运维管理

#### 回滚 (Rollback)

当发现某个技能版本有严重问题时，可立即回滚。

```json
POST /v1/skills/releases/{release_id}/rollback
{
  "reason": "导致页面卡死"
}
```

#### 健康检查

查询技能发布的健康状态（基于最近的执行统计）。

```bash
GET /v1/skills/releases/{release_id}/health
```

---

## 4. 操作指南

### 4.1 生命周期管理流程

标准的手动干预流程如下：

1.  **Draft (草稿)**: 系统自动提取出 `SkillCandidate`，状态为 `PENDING`。
2.  **Review (审核)**: 运维/开发人员通过 API 查看 Candidate 详情（包含 Trace 和截图）。
3.  **Evaluate (评估)**: 确认无误后，调用 Evaluate 接口打分。
4.  **Promote (发布)**: 调用 Promote 接口发布为 `CANARY`。
5.  **Observe (观察)**: 经过一段时间运行，监控其健康度。
6.  **Stabilize (固化)**: 确认稳定后，再次 Promote 为 `STABLE`。

### 4.2 监控与告警

建议重点监控以下指标：

*   **Learning Queue Size**: 待处理的历史记录积压量。
*   **Candidate Acceptance Rate**: 候选技能被采纳/拒绝的比例。
*   **Skill Execution Success Rate**: 已发布技能的运行成功率。
*   **Auto Rollback Events**: 自动回滚触发次数（严重告警）。

### 4.3 故障排查

#### 问题：学习未触发 (No Candidates Generated)
*   **检查 `learn=true`**: 确认 Agent 发送请求时是否携带了此参数。
*   **检查只读过滤**: 确认操作是否全是 `get`, `wait`, `snapshot` 等只读命令，这些会被自动过滤。
*   **检查步数**: 默认阈值下，单步操作得分可能不足以生成技能。尝试在一个 `exec_batch` 或连续会话中执行更多步骤。
*   **检查日志**: 查看 Bay 服务日志中 `BrowserLearningScheduler` 的输出。

#### 问题：评分过低
*   **原因**: 操作序列过短或包含大量无效等待。
*   **解决**: 增加有效操作密度，或在配置中临时降低 `score_threshold`。

#### 问题：发布失败
*   **原因**: 自动回放验证（Replay Verification）失败。
*   **解决**: 检查操作是否依赖特定的页面上下文（如特定的 URL 或登录状态），而回放环境未满足这些条件。

---

## 5. 最佳实践

### 5.1 标签（Tags）使用策略

良好的标签习惯是高效管理技能库的关键。推荐采用 **`维度:值`** 的格式：

*   **业务域**: `domain:e-commerce`, `domain:admin`
*   **功能类**: `category:login`, `category:checkout`
*   **动作类**: `action:fill_form`, `action:navigate`

示例：`tags="domain:crm,category:customer,action:create"`

### 5.2 渐进式发布策略

永远不要直接发布 `STABLE` 版本，除非是紧急修复。
1.  **Canary**: 先发金丝雀版本，让 10%-20% 的流量（或特定测试流量）使用。
2.  **Observation**: 观察至少 24 小时（默认配置窗口）。
3.  **Stable**: 指标正常自动晋升或人工确认。

### 5.3 技能颗粒度控制

*   **避免过大**: 不要试图录制整个“从登录到下单”的长流程，极其容易因由于页面微小变化而失败。
*   **避免过小**: 单个点击操作没必要做成技能。
*   **推荐粒度**: **“语义完备的最小单元”**。例如：“登录”、“填写收货地址”、“添加到购物车”。

## 相关文档

| 文档 | 说明 |
|------|------|
| [浏览器 Self-Update 功能介绍](browser_self_update_intro_zh.md) | 核心概念、架构设计与工作原理 |
| [Bay API v1 参考](bay_api_v1.md) | Bay 控制面 API（含浏览器接口 §2.3/2.4） |
| [Bay 错误码参考](bay_error_codes.md) | 错误码与排错指南 |
| [Bay 抽象实体](bay_abstract_entities.md) | Sandbox、Cargo 等核心概念 |
| [`pkgs/gull/README.md`](pkgs/gull/README.md) | Gull 项目简介 |
