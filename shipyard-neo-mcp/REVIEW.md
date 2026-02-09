# Shipyard Neo MCP Server 代码审查清单

> **审查目标**: `shipyard-neo-mcp` 的工具层稳定性、输入安全性与长期运行可靠性  
> **审查范围**: `src/shipyard_neo_mcp/server.py` 与 `tests/`  
> **最后更新**: 2026-02-09

---

## 总体评价

MCP Server 已覆盖 sandbox + history + skill lifecycle 的核心工具集，功能完整度高，便于 Agent 快速接入。

当前核心稳定性问题已完成修复（参数校验、输出截断、错误细节透出、缓存有界化），并已补齐对应测试。

## 修复状态（2026-02-09）

- [x] 工具参数支持运行时校验，缺参/类型错返回 `**Validation Error:** ...`。
- [x] 工具输出统一截断，避免长输出导致上下文爆炸。
- [x] `BayError` 返回包含 `code + message + details`（details 截断）。
- [x] `_sandboxes` 缓存改为有界缓存，超过上限按 LRU 淘汰。
- [x] 新增防御性测试（校验/截断/缓存淘汰），MCP lint + test 通过。

---

## 已修复的高优先级项

### 1. 工具参数缺少运行时校验，异常信息不具可操作性（已修复）

**文件**:
- `src/shipyard_neo_mcp/server.py:490`
- `src/shipyard_neo_mcp/server.py:535`
- `src/shipyard_neo_mcp/server.py:570`
- `src/shipyard_neo_mcp/server.py:843`

**问题**:
- 代码大量使用 `arguments["xxx"]`，缺字段时抛 `KeyError`，最终返回 `**Error:** 'sandbox_id'` 这类低质量错误信息。
- 虽然 `list_tools` 提供了 JSON schema，但运行时仍应做防御式校验。

**修复建议**:
1. 为每个工具定义轻量参数解析函数（必填字段、类型、边界值）。
2. 将参数错误统一返回为明确可读的 message（例如“missing required field: sandbox_id”）。
3. 对 `timeout/limit/offset` 做最小范围约束，避免将非法值透传到底层。

**验收标准**:
- 缺少必填参数时返回结构化、可读的错误，而不是 Python 异常字面量。

---

### 2. 输出内容无上限，易造成上下文爆炸与性能风险（已修复）

**文件**:
- `src/shipyard_neo_mcp/server.py:507`
- `src/shipyard_neo_mcp/server.py:552`
- `src/shipyard_neo_mcp/server.py:580`
- `src/shipyard_neo_mcp/server.py:673`

**问题**:
- `execute_python` / `execute_shell` / `read_file` / `get_execution` 直接返回完整内容，无截断策略。
- 当输出或文件很大时，会显著增加 token 成本、响应时延，甚至影响 Agent 稳定性。

**修复建议**:
1. 引入统一截断函数（例如默认 8KB~32KB）。
2. 在响应中附带“已截断”提示和原始长度。
3. 对代码块输出启用安全截断，避免破坏 markdown 结构。

**验收标准**:
- 大输出场景不会导致单次工具响应失控。

---

## 已修复的中优先级项

### 3. `_sandboxes` 全局缓存无淘汰机制（已修复）

**文件**:
- `src/shipyard_neo_mcp/server.py:27`
- `src/shipyard_neo_mcp/server.py:437`
- `src/shipyard_neo_mcp/server.py:461`

**问题**:
- 缓存会在运行期持续增长，仅在进程退出时清空。
- 长时间运行或高频创建 sandbox 的 Agent 场景可能造成不必要内存增长。

**修复建议**:
1. 增加简单 LRU/TTL 策略（保持轻量，不引入第三方依赖）。
2. 删除 sandbox 时已 `pop`，可进一步在异常路径（404）自动剔除缓存。

---

### 4. `BayError` 信息丢失 code/details，排障信息不足（已修复）

**文件**:
- `src/shipyard_neo_mcp/server.py:841`

**问题**:
- 当前只返回 `e.message`，丢失 `code/details`。
- 对策略型错误（如 `validation_error`, `conflict`）不利于 Agent 决策。

**修复建议**:
- 输出 `code + message + 关键 details`（可截断），提升可观测性。

---

### 5. 测试覆盖偏 happy path，缺少防御性场景（已修复）

**文件**:
- `tests/test_server.py`

**缺口**:
- 缺少“缺参数/错类型/极大输出/缓存剔除”的测试。
- 缺少“BayError details 展示”的断言。

**修复建议**:
1. 为参数校验新增参数化测试。
2. 新增大输出截断测试。
3. 新增缓存清理策略测试。

---

## 🟡 低优先级审查项

### 6. `call_tool` 分支链过长，后续扩展成本偏高

**文件**:
- `src/shipyard_neo_mcp/server.py:454` 之后

**建议**:
- 逐步改为“工具名 -> handler 函数”注册表，降低冲突和维护成本。

---

## 📋 后续优化大纲（建议顺序）

1. 保留 #6（`call_tool` 分发重构）作为结构优化项。

---

## ✅ 通过项（本轮确认）

- History + Skill Lifecycle 工具集已完整接入。
- `get_config()` 支持 `SHIPYARD_*` 与 `BAY_*` 回退。
- 基础单测已经覆盖工具存在性和关键 happy path。
