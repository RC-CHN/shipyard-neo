# Skill Evolution: SDK & MCP Integration Design

> 配套文档 · 与 `skill-evolution-intelligence.md` 联合阅读

---

## 一、设计原则

**中心思想文档**已确立：人只提供意图，AI 负责策略。

本文档回答：这个原则如何在 SDK 和 MCP 层落地？

具体地：

- SDK 的 `SkillManager` 当前是显式生命周期管理（CRUD），需要演变为**意图驱动接口**
- MCP 的 skill 工具当前是手动操作封装，需要演变为**AI agent 的自然语言接口**
- Bay 需要新增**目标注册**和**演化调度**能力作为内部基础设施

---

## 二、当前状态（Before）

### SDK 层

```python
# 当前使用方式：agent 必须管理完整的生命周期
payload = await skill_manager.create_payload(data={...})
candidate = await skill_manager.create_candidate(
    skill_key="github-get-stars",
    payload_ref=payload.payload_ref,
    skill_type="browser",
)
eval_result = await skill_manager.evaluate_candidate(
    candidate_id=candidate.candidate_id,
    passed=True,
    score=0.92,
    benchmark_id="browser-replay-v1",
)
release = await skill_manager.promote_candidate(
    candidate_id=candidate.candidate_id,
    stage="canary",
)
```

问题：agent 必须理解系统内部状态，手动协调每一步。评估标准由人硬编码（`passed=True`, `score=0.92`），没有演化反馈机制。

### MCP 层

```
tools available:
  - create_skill_candidate    # 创建候选
  - evaluate_skill_candidate  # 评估
  - promote_skill_candidate   # 晋升
  - rollback_skill_release    # 回滚
```

问题：工具是操作（operation），不是意图（intent）。AI agent 必须知道"我现在应该调用哪一步"。

---

## 三、目标状态（After）

### 3.1 SDK 新接口

新增三个意图驱动方法，保留现有 CRUD 接口（向后兼容）：

```python
class SkillManager:

    # ────────────────────────────────────────────────
    # 意图接口（新增）
    # ────────────────────────────────────────────────

    async def declare_goal(
        self,
        skill_key: str,
        goal: str,
    ) -> GoalDeclaration:
        """
        声明某个 skill 的目标。

        Bay 收到后：
        1. 存储 goal，生成初始 rubric（goal-conditioned evaluator）
        2. 如果该 skill_key 已有 goal，更新并触发重新评估
        3. 如果尚无 active release，标记为"待演化"

        返回 GoalDeclaration：包含 goal_id 和 Bay 生成的初始 rubric 摘要。

        agent 不需要关心 rubric 的内容，rubric 是 AI 的策略，不是 API 参数。
        """

    async def get_active(
        self,
        skill_key: str,
    ) -> SkillView | None:
        """
        获取当前 active 的 skill release。

        返回 SkillView：包含 skill 的自然语言描述、preconditions、
        postconditions、以及 SKILL.md 格式内容（如果 release 存在）。

        agent 读取这个内容作为执行指导，不需要理解 release_id、
        candidate_id 等内部状态。
        """

    async def report_outcome(
        self,
        skill_key: str,
        release_id: str,
        outcome: Literal["success", "failure", "partial"],
        reasoning: str,
        execution_id: str | None = None,
        signals: dict[str, Any] | None = None,
    ) -> None:
        """
        向演化系统汇报 skill 执行结果。

        reasoning 是必填项——agent 必须解释为什么这次执行成功或失败。
        这条 reasoning 会进入演化循环：
        - 成功案例 → 作为 MAP-Elites 的正向信号
        - 失败案例 → 触发 ReflectionMemory 存储，下次突变时注入上下文

        signals 是可选的定量信号，例如：
          {"page_load_time_ms": 1200, "element_found": true, "retry_count": 0}
        Bay 的 goal-conditioned evaluator 会根据 rubric 决定如何使用这些信号。

        agent 不需要知道：
        - 如何计算 score
        - 是否触发突变
        - 是否更新 archive
        这些都是 Bay 的策略，由 Bay 的演化调度器决定。
        """

    # ────────────────────────────────────────────────
    # 现有 CRUD 接口（保留，向后兼容）
    # ────────────────────────────────────────────────

    async def create_payload(self, ...) -> PayloadRef: ...
    async def create_candidate(self, ...) -> SkillCandidateRef: ...
    async def evaluate_candidate(self, ...) -> SkillEvaluationRef: ...
    async def promote_candidate(self, ...) -> SkillReleaseRef: ...
    async def rollback_release(self, ...) -> SkillReleaseRef: ...
    async def get_release_health(self, ...) -> ReleaseHealth: ...
```

### 3.2 新增数据类型

```python
@dataclass
class GoalDeclaration:
    goal_id: str
    skill_key: str
    goal: str
    rubric_summary: str   # Bay 生成的初始 rubric 的自然语言摘要
                          # 不是完整 rubric，是给人/agent 看的概要

@dataclass
class SkillView:
    skill_key: str
    release_id: str
    version: int
    stage: str            # "canary" | "stable"
    goal: str | None      # 如果 goal 已声明
    content: str          # SKILL.md 格式内容，agent 直接使用
    summary: str | None
    preconditions: list[str]
    postconditions: list[str]
```

---

## 四、MCP 新工具

### 4.1 工具定义

```python
# 现有工具（保留）
create_skill_candidate    → 用于手动/高级场景
evaluate_skill_candidate  → 用于手动评估
promote_skill_candidate   → 用于手动晋升
rollback_skill_release    → 用于手动回滚

# 新增工具（意图驱动）
get_active_skill          → 获取当前 active skill 的内容
declare_skill_goal        → 声明 skill 目标
report_skill_outcome      → 汇报执行结果
```

### 4.2 工具描述（面向 AI agent）

```python
TOOLS = [
    {
        "name": "get_active_skill",
        "description": (
            "Get the currently active skill for a given skill_key. "
            "Returns the skill content in SKILL.md format that the agent "
            "should follow when executing the skill. "
            "Returns null if no active skill exists yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_key": {
                    "type": "string",
                    "description": "The skill identifier, e.g. 'github-get-stars'"
                }
            },
            "required": ["skill_key"]
        }
    },
    {
        "name": "declare_skill_goal",
        "description": (
            "Declare the goal for a skill. The system will use this goal to "
            "automatically generate evaluation criteria and drive skill evolution. "
            "Call this once when introducing a new skill type, or when the goal changes. "
            "The agent does not need to specify how to evaluate — the system handles that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_key": {"type": "string"},
                "goal": {
                    "type": "string",
                    "description": (
                        "Natural language description of what this skill should accomplish. "
                        "Be specific about the desired outcome, not the steps. "
                        "Example: 'Navigate to a GitHub repository page and return the current star count as an integer.'"
                    )
                }
            },
            "required": ["skill_key", "goal"]
        }
    },
    {
        "name": "report_skill_outcome",
        "description": (
            "Report the outcome of executing a skill. This feeds into the skill "
            "evolution system — failures generate reflection memory that improves "
            "future mutations, successes reinforce the current approach. "
            "The reasoning field is required: explain why the execution succeeded or failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_key": {"type": "string"},
                "release_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure", "partial"],
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Why did this execution succeed or fail? "
                        "Be specific: what worked, what broke, what was unexpected. "
                        "This reasoning is stored and used in future evolution cycles."
                    )
                },
                "execution_id": {"type": "string"},
                "signals": {
                    "type": "object",
                    "description": "Optional quantitative signals from the execution"
                }
            },
            "required": ["skill_key", "release_id", "outcome", "reasoning"]
        }
    }
]
```

---

## 五、全栈集成图

```
┌──────────────────────────────────────────────────────────────────┐
│  AI Agent (Claude / camel WebVoyager / any MCP client)           │
│                                                                  │
│  1. declare_skill_goal("github-get-stars", goal="...")           │
│  2. skill = get_active_skill("github-get-stars")                 │
│  3. execute skill.content against sandbox                        │
│  4. report_skill_outcome(..., outcome="success", reasoning="...") │
└────────────────────────────┬─────────────────────────────────────┘
                             │ MCP protocol
┌────────────────────────────▼─────────────────────────────────────┐
│  shipyard-neo-mcp                                                │
│  handlers/skills.py                                              │
│  handle_declare_skill_goal()                                     │
│  handle_get_active_skill()                                       │
│  handle_report_skill_outcome()                                   │
└────────────────────────────┬─────────────────────────────────────┘
                             │ HTTP / SDK
┌────────────────────────────▼─────────────────────────────────────┐
│  shipyard-neo-sdk                                                │
│  SkillManager                                                    │
│  .declare_goal()  .get_active()  .report_outcome()               │
└────────────────────────────┬─────────────────────────────────────┘
                             │ REST API
┌────────────────────────────▼─────────────────────────────────────┐
│  Bay (FastAPI)                                                   │
│                                                                  │
│  POST /v1/skills/goals          ← declare_goal                  │
│  GET  /v1/skills/{key}/active   ← get_active                    │
│  POST /v1/skills/outcomes       ← report_outcome                │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │  GoalRegistry        │  │  EvolutionScheduler              │  │
│  │  goal → rubric       │  │  收集 outcome →                  │  │
│  │  (goal-conditioned   │  │  触发 SkillMutationAgent         │  │
│  │   evaluator)         │  │  更新 MAP-Elites archive         │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  现有基础设施（不变）                                          │ │
│  │  SkillCandidate · SkillEvaluation · SkillRelease             │ │
│  │  ArtifactBlob · BrowserLearningScheduler                    │ │
│  └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

---

## 六、Bay 新增内部组件

### GoalRegistry

职责：存储 goal，生成并缓存 rubric，响应 goal 变更。

```
POST /v1/skills/goals
  body: { skill_key, goal }
  → 存储 goal
  → 调用 LLM 生成 rubric（goal-conditioned evaluator）
  → rubric 存储在 ArtifactBlob（kind="skill_rubric"）
  → 返回 goal_id + rubric_summary
```

rubric 的生成逻辑由 AI 决定，不由 API 参数控制。Bay 的 `GoalConditionedEvaluator` 读取 goal，自主决定：
- 哪些维度需要确定性检查（`check_type: deterministic`）
- 哪些维度需要 LLM judge（`check_type: llm_judge`）
- 各维度权重

### EvolutionScheduler

职责：从 `report_outcome` 的信号流中驱动演化循环。

行为由 AI 自主决定，人只控制触发频率上限（资源约束）：

```
outcome 流入
  → GoalConditionedEvaluator 评估（对照 rubric + signals）
  → fitness 更新 MAP-Elites archive
  → 如果 outcome == "failure"
      → ReflectionMemory.store(skill_key, reasoning, execution_trace)
  → 达到演化触发条件（AI 决定）
      → SkillMutationAgent.generate_mutation(
            current_skill,
            reflection_memory,
            meta_prompt_archive,
        )
      → 新 candidate → 走现有 evaluation → release 流程
```

---

## 七、camel 集成点

camel 的 `TaskVerifier` 是天然的 `report_skill_outcome` 数据源。

```python
# camel/examples/toolkits/browser_skills_example/run_webvoyager_tasks.py
# TaskVerifier 已有的逻辑：
class TaskVerifier:
    async def verify(self, task: str, trajectory: list) -> VerificationResult:
        # LLM judge: 判断任务是否完成
        ...

# 集成后，在 WebVoyagerRunner 的结束处：
async def run_task(self, task: WebVoyagerTask) -> TaskResult:
    # ... 执行 ...
    result = await self.execute(task)

    # 新增：汇报结果给演化系统
    skill = await self.skill_manager.get_active(task.skill_key)
    if skill:
        verification = await self.verifier.verify(task.goal, result.trajectory)
        await self.skill_manager.report_outcome(
            skill_key=task.skill_key,
            release_id=skill.release_id,
            outcome="success" if verification.passed else "failure",
            reasoning=verification.explanation,   # TaskVerifier 已有的自然语言解释
            execution_id=result.execution_id,
            signals={
                "steps_taken": len(result.trajectory),
                "retry_count": result.retry_count,
                "time_ms": result.execution_time_ms,
            },
        )
```

`TaskVerifier` 的 `explanation` 字段（LLM 对任务完成情况的自然语言描述）直接成为 `ReflectionMemory` 的输入——零额外工作。

---

## 八、Developer Experience 对比

### 8.1 首次引入新技能

**Before（当前）：**
```python
# 开发者必须自己决定：评估逻辑、阈值、晋升时机
payload = await sm.create_payload(data={"steps": [...]})
candidate = await sm.create_candidate(skill_key="github-get-stars", ...)
# 手动写评估逻辑
await sm.evaluate_candidate(candidate_id=..., passed=True, score=0.9)
await sm.promote_candidate(candidate_id=..., stage="canary")
```

**After（新）：**
```python
# 开发者只声明意图
await sm.declare_goal(
    skill_key="github-get-stars",
    goal="Navigate to a GitHub repository page and return the current star count as an integer."
)
# 系统自动生成 rubric、等待演化产生候选、自动评估晋升
```

### 8.2 日常执行

**Before：**
```python
# 每次执行都要手动查询 release，手动获取 payload
release = await sm.get_active_release(skill_key="github-get-stars")
candidate = await sm.get_candidate(candidate_id=release.candidate_id)
payload = await sm.get_payload(payload_ref=candidate.payload_ref)
# 然后执行 payload["steps"]
```

**After：**
```python
# 一次调用获取所有执行所需信息
skill = await sm.get_active(skill_key="github-get-stars")
if skill:
    # skill.content 是 SKILL.md 格式，直接给 LLM 执行
    result = await agent.execute_skill(skill.content, sandbox_id=...)
```

### 8.3 执行结果反馈

**Before：**
无机制。执行完就结束，没有反馈回路。

**After：**
```python
await sm.report_outcome(
    skill_key="github-get-stars",
    release_id=skill.release_id,
    outcome="failure",
    reasoning=(
        "The repository page layout has changed. "
        "The star count element previously at '.social-count' "
        "is now inside a React component that renders asynchronously. "
        "The skill needs to wait for dynamic content to load."
    ),
    execution_id=result.execution_id,
)
# 系统自动：生成 reflection，下次突变时注入上下文
# 开发者不需要做任何其他事情
```

---

## 九、API 端点设计

新增三个端点，挂载在 `/v1/skills/` 下：

```
POST /v1/skills/goals
  Request:  { skill_key: str, goal: str }
  Response: { goal_id: str, rubric_summary: str }

GET  /v1/skills/{skill_key}/active
  Response: {
    skill_key: str,
    release_id: str,
    version: int,
    stage: "canary" | "stable",
    goal: str | null,
    content: str,          # SKILL.md 格式
    summary: str | null,
    preconditions: list[str],
    postconditions: list[str],
  } | null

POST /v1/skills/outcomes
  Request:  {
    skill_key: str,
    release_id: str,
    outcome: "success" | "failure" | "partial",
    reasoning: str,        # 必填
    execution_id: str | null,
    signals: object | null,
  }
  Response: { accepted: true }
```

Bay 内部对 `outcome` 的处理策略（何时触发突变、如何聚合信号）由演化调度器的 AI 决定，不通过 API 暴露为可配置参数。

---

## 十、openspec 更新方向

以下能力需要新增到 openspec specs 中（Phase 1 落地时）：

1. **`skill-goal-registry`** — 新 spec：声明 goal、生成 rubric、goal 演化追踪
2. **`skill-outcome-reporting`** — 新 spec：outcome 汇报、reflection memory 行为
3. **`agent-skill-lifecycle`** — 现有 spec 扩展：`get_active_skill` 端点的合约

---

*last updated: 2026-03-06*
*status: living document*
*paired with: plans/skill-evolution-intelligence.md*
