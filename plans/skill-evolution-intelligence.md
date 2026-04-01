# Skill Evolution Intelligence

> 中心设计文档 · 持续更新

---

## 一、核心主张

**人负责意图，AI 负责策略。**

这不是一个功能需求，而是一个开发哲学。

在 AI-native 的系统里，人类工程师的职责是定义**边界**——什么可以被改变、什么信号是可观测的、什么行为是不允许的。在边界之内，AI 应当自主地设计、执行、评估并演化自己的策略，而不是执行人硬编码的规则。

一切"我觉得这个场景应该设 `mutation_prob = 0.3`"这类判断，都是范畴错误。它把策略写死在了基础设施里。策略应当是 AI 的职责，它应该能够被观察、被质疑、被演化。

---

## 二、我们要解决的问题

当前 Shipyard 的 skill 体系（遵循 Anthropic Agent Skills 规范，SKILL.md 格式）存在一个根本性的静态缺陷：

**skill 被创造出来，但不会自我改进，也不知道自己是否正在变差。**

具体表现：

- Skill 从 browser execution trace 中提取，提取质量取决于提取规则的准确性
- Skill 的评估是规则打分（`_score_segment`），不理解 skill 的语义
- 网站 UI 改版后，skill 中的步骤会静默失效，没有感知机制
- 多个语义相似的 skill 版本同时存在，没有竞争/淘汰机制
- Skill 的好坏标准由人在代码里定义，无法适应不同类型的 skill

更深层的问题：**当前系统是单向流水线，不是演化系统。**

```
现状：execution → extract → evaluate → release
                                          ↑ 终点，无反馈

目标：execution → extract → [演化循环] → 持续改进的 skill 库
```

---

## 三、研究基础

以下论文构成本方案的理论支撑，按重要性排序。

### AlphaEvolve（Google DeepMind, 2025）
**最直接的参考框架**

核心机制：LLM 作为变异算子，读取历史版本和评估结果，生成有依据的 diff（而非随机变异）。维护两个并行演化的数据库：solution archive（skill 变体）和 meta-prompt archive（"如何生成更好 skill"的指令本身）。

对本项目的贡献：
- **两数据库并行演化**的结构：skill 和生成 skill 的策略同时改进
- **MAP-Elites + island model**：不只保留"最优"，保留"多样性"
- **LLM 生成 feedback 作为额外评估维度**：对无法自动评估的任务的补充方案

局限：假设评估函数是自动化、可量化的。UI 美化、项目设计等创造性任务不在其原始范围内。本项目通过 goal-conditioned rubric 生成机制扩展了这个边界。

### Darwin Gödel Machine（UBC / Sakana AI, 2025）
**Agent 修改自身策略的极限形态**

DGM 不优化某个解，而是优化 agent 自己的代码。它维护一个 agent 变体的 archive，不断让 agent 修改自身，并在真实任务上评估。关键发现：agent 自发地给自己加上了记录历史尝试的 memory、生成多解取最优的 ensemble 策略——这些都不是人设计的。

对本项目的贡献：
- **策略本身是可演化的**，不只是 skill 内容
- **open-ended 探索比收敛到单一最优更有价值**，特别是对创造性任务
- **Archive 保留 stepping stones**（当前非最优但未来有用的中间状态）

### TextGrad（Stanford / CZ Biohub, 2024）
**LLM 系统的自动微分**

将 LLM 系统建模为计算图，文本反馈作为"梯度"，沿计算图反向传播更新 prompt/instruction。SKILL.md 是可优化的变量，执行结果是 loss 的来源。

对本项目的贡献：
- 把 SKILL.md 优化问题明确地建模为**梯度优化的自然语言类比**
- 每次执行失败都产生一个"文本梯度"：描述为什么失败、应该怎么改

### Reflexion（NeurIPS, 2023）
**失败比成功更有价值**

不更新模型权重，而是把失败轨迹转换为自然语言反思，存入 episodic memory，作为下次尝试的上下文。实验表明这比单纯的 episodic memory 多 8% 绝对性能提升。

对本项目的贡献：
- **每次 skill 失败应生成 verbal reflection，而不只是记录 fail 状态**
- 反思记忆跨 session 积累，下一个同类 skill candidate 到达时注入上下文

---

## 四、核心架构

### 基本原则

人只做一件事：**表达意图（goal）**。

系统暴露三类硬性约束（基础设施，不可演化）：

```
1. 执行能力    execute(skill, sandbox) → signals
2. 存储接口    store/retrieve(archive)
3. 变异边界    什么可以被改（SKILL.md 内容）
               什么不可以被改（执行沙箱、其他 skill、安全边界）
```

**边界内的一切，包括策略本身，都由 AI 决定并留下 reasoning。**

### 系统分层

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 0: Human Intent                                      │
│  goal: str  ← 人唯一的输入                                   │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  Layer 1: AI Strategy Engine（可演化）                       │
│                                                             │
│  ┌─────────────────┐    ┌───────────────────────────────┐   │
│  │  Skill Archive  │    │  Meta-Prompt Archive          │   │
│  │  MAP-Elites     │    │  "如何改进 skill 的指令"       │   │
│  │  (skill_key ×   │    │  同样用 MAP-Elites 管理        │   │
│  │  behavior_dim)  │    │  好的指令 → 更高选中概率       │   │
│  └────────┬────────┘    └──────────────┬────────────────┘   │
│           │ sample skill               │ sample mutation    │
│           └──────────────┬─────────────┘                    │
│                          ▼                                  │
│                  LLM Mutation Agent                         │
│                  生成 SKILL.md diff                          │
│                  留下 reasoning                              │
└────────────────────────┬────────────────────────────────────┘
                         │ new candidate
┌────────────────────────▼────────────────────────────────────┐
│  Layer 2: Infrastructure（固定）                             │
│  execute → signals → evaluate → archive update             │
└─────────────────────────────────────────────────────────────┘
```

### 评估机制：goal-conditioned rubric

评估标准不是预先定义的，而是从 goal 中动态生成：

```
goal (str)
  ↓  LLM
rubric: [
  {criterion: str, check_type: "deterministic|llm_judge", weight: float}
]
  ↓  执行 skill
execution signals
  ↓  LLM judge（按 rubric 逐条评估）
score_vector: {c1: 0.9, c2: 0.7, c3: 1.0}
  ↓
fitness: float（加权聚合）
```

对确定性任务（获取数据、执行操作）：rubric 大部分是 `check_type: deterministic`，
AI 自动识别并使用外部信号验证。

对创造性任务（UI 设计、架构规划）：rubric 以 `check_type: llm_judge` 为主，
`target` 不是收敛到最优，而是维持多样性 archive。

**Rubric 本身也被存储并演化**：如果某个 rubric 和后续外部反馈一致性高，
它在 meta-prompt archive 里的分数上升；如果它系统性地漏掉某个维度，分数下降。

### 演化循环

```python
# 这不是实现代码，是逻辑描述
# 具体参数全部由 AI 在运行时决定

while True:
    # 1. 采样：从 archive 选一个 skill + 一条 mutation instruction
    skill = archive.sample(strategy=AI_DECIDES)
    instruction = meta_prompt_archive.sample(strategy=AI_DECIDES)

    # 2. 变异：LLM 生成 diff + reasoning
    new_skill, reasoning = llm.mutate(skill, instruction, history)

    # 3. 执行并评估
    signals = execute(new_skill)
    rubric = llm.generate_rubric(skill.goal)      # 从 goal 生成
    fitness = llm.evaluate(signals, rubric)

    # 4. 更新 archive
    archive.update(new_skill, fitness, reasoning)  # MAP-Elites 竞争
    meta_prompt_archive.update(instruction, outcome=fitness_delta)

    # 5. 失败时生成 reflection（Reflexion 机制）
    if fitness < incumbent_fitness:
        reflection = llm.reflect(skill, new_skill, signals, rubric)
        reflection_memory.store(skill.key, reflection)
```

---

## 五、与现有系统的关系

### 当前 Bay 已有的（保留并扩展）

| 现有组件 | 在新系统中的角色 |
|---|---|
| `SkillCandidate` 表 | Skill Archive 的持久化层 |
| `SkillEvaluation` 表 | 评估结果记录，扩展为存储 rubric 和 reasoning |
| `SkillRelease` 表 | Archive incumbent 的生产版本 |
| `BrowserLearningScheduler` | 演化循环的调度基础设施 |
| `ArtifactBlob` | Skill payload 和 reflection memory 的存储 |
| `LlmAssistedExtractionStrategy` | 保留，作为 skill 初始生成的入口 |

### 需要新建的

| 新组件 | 职责 |
|---|---|
| `SkillMutationAgent` | 读取 archive + meta-prompt，调用 LLM 生成 diff |
| `GoalConditionedEvaluator` | 从 goal 生成 rubric，执行评估 |
| `ReflectionMemory` | per-skill-key 的失败反思存储 |
| `MetaPromptArchive` | mutation instruction 的演化数据库 |
| behavior descriptor 列 | `SkillCandidate` 表新增，用于 MAP-Elites 维度 |

### 不需要重写的

Bay 的数据模型骨架是合适的。演化逻辑是叠加在现有调度层之上的新阶段，
不替换现有的 extraction → evaluation → release 流水线，
而是在流水线末尾增加一个**演化反馈环**。

---

## 六、关于"度"的把握

放权 AI 不等于没有约束。需要保持清醒的几个边界：

**必须硬编码的**（基础设施层，不交给 AI）：
- SKILL.md 格式规范（Anthropic Agent Skills 标准，行业互操作基础）
- 执行沙箱的安全边界
- 变异不能修改其他 skill 或基础设施代码
- 每轮演化的最大 LLM 调用预算（资源约束）

**不应该硬编码的**（策略层，交给 AI）：
- 评估标准和权重
- 何时触发变异、变异幅度
- Archive 组织方式（behavior descriptor 的语义）
- 收敛 vs 多样化的取舍
- Meta-prompt 的内容和演化方向

**关键设计原则**：AI 做的每个策略决策必须附带自然语言 reasoning，
存入 archive。这不是监控，而是为了让 AI 自己能在下一轮读取历史并质疑自己的判断。
**没有 reasoning 的策略决策不被接受。**

---

## 七、开发方向与阶段划分

### Phase 1：建立演化基础设施
- 为 `SkillCandidate` 增加 behavior descriptor 维度
- 实现 `ReflectionMemory`（基于现有 `ArtifactBlob`）
- 实现 `GoalConditionedEvaluator` 替换 `_score_segment`
- 在 `BrowserLearningScheduler` 末尾加入 reflection 生成步骤

### Phase 2：引入 LLM 变异循环
- 实现 `SkillMutationAgent`
- 实现 MAP-Elites archive 竞争逻辑
- 演化循环接入调度器（独立于 extraction 流水线）

### Phase 3：Meta-Prompt 演化
- 实现 `MetaPromptArchive`
- Mutation instruction 开始随使用结果更新评分
- 观察 AI 自发涌现的 mutation 策略类型

### Phase 4：策略引擎自演化（DGM 方向）
- 评估 AI 是否能修改自己的演化策略代码
- 引入 open-ended exploration 机制
- 评估 rubric 演化的可靠性

---

## 八、一个值得记住的判断标准

**如果一个参数需要人来设定它的数值，那它就不应该是参数，它应该是 AI 的一个判断。**

系统设计中每出现一个 magic number，就问自己：
"为什么是这个数而不是另一个数？"
如果答案是"因为我觉得合适"，那它就应该被 AI 的 reasoning 替代。

---

*last updated: 2026-03-06*
*status: living document*
