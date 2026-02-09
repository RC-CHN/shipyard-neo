# Shipyard Neo SDK 代码审查清单

> **审查目标**: `shipyard-neo-sdk` 当前实现的可靠性、可维护性与错误可观测性  
> **审查范围**: `shipyard_neo/` 与 `tests/`  
> **最后更新**: 2026-02-09

---

## 总体评价

SDK 的 API 组织清晰（`BayClient` / `Sandbox` / `CargoManager` / `SkillManager`），history 与 skill lifecycle 能力可用，且本次已补齐关键可靠性修复与测试闭环。

## 修复状态（2026-02-09）

- [x] `max_retries` 已在 `HTTPClient.request()` 与 `download()` 生效，支持有界指数退避。
- [x] 重试策略已做幂等保护：`POST` 仅在携带 `idempotency_key` 时允许重试。
- [x] 非 JSON 错误响应已支持按 HTTP status fallback 映射语义化异常。
- [x] 新增可靠性测试：`tests/test_http_reliability.py` 覆盖重试触发边界与非 JSON 映射。
- [x] SDK lint + test 通过（`ruff check` / `pytest`）。

---

## 已修复的高优先级项

### 1. `max_retries` 参数未生效（已修复）

**文件**:
- `shipyard_neo/client.py:36`
- `shipyard_neo/_http.py:34`
- `shipyard_neo/_http.py:47`

**问题**:
- `BayClient` 暴露了 `max_retries`（并支持 `BAY_MAX_RETRIES`），但 `HTTPClient` 内仅保存 `_max_retries`，请求链路没有任何重试逻辑。
- 这会让调用方误以为 SDK 对瞬时网络抖动有保护，实际上没有。

**修复建议**:
1. 在 `HTTPClient.request()` 中实现轻量重试（仅对幂等/可重试错误触发，例如连接错误、5xx、429）。
2. 对 `POST` 默认禁用自动重试，除非显式传入 `idempotency_key`。
3. 增加指数退避（最小实现即可，无需引入新依赖）。

**验收标准**:
- 配置 `max_retries=3` 时，瞬时 5xx/网络错误能自动重试并成功。
- 非幂等请求不会被误重试。

---

### 2. 非 JSON 错误响应会丢失状态语义（已修复）

**文件**:
- `shipyard_neo/_http.py:137`
- `shipyard_neo/_http.py:143`
- `shipyard_neo/errors.py:170`

**问题**:
- 当上游返回 HTML / 空 body / 代理错误页时，`response.json()` 失败后 `body = {}`。
- `raise_for_error_response()` 依赖 `error.code`，缺失时统一抛 `BayError`，无法映射到 `NotFoundError` / `UnauthorizedError` 等具体类型。

**修复建议**:
1. 在 `raise_for_error_response` 增加“按 HTTP status fallback 映射”的分支。
2. 非 JSON 场景保留 `response.text` 片段作为 message（截断到安全长度）。
3. upload/download 路径也复用同样策略，避免行为分裂。

**验收标准**:
- 404 非 JSON 响应仍抛 `NotFoundError`。
- 502 非 JSON 响应仍抛可识别错误（至少 message 可读）。

---

## 已修复的中优先级项

### 3. 错误处理路径缺少针对性测试（已修复）

**文件**:
- `tests/test_http_reliability.py`

**缺口**:
- 未覆盖“非 JSON 错误响应映射”。
- 未覆盖“重试策略触发条件与终止条件”。

**修复建议**:
1. 新增 `_http` 级单测：非 JSON 404/500 的映射断言。
2. 新增重试行为测试：重试成功、超过次数失败、POST+无幂等键不重试。

---

## 🟠 中优先级审查项（剩余）

### 4. `SkillManager` 类型约束较弱（未完成）

**文件**:
- `shipyard_neo/skills.py:19`

**问题**:
- `SkillManager.__init__(self, http)` 未声明类型，降低 IDE/静态检查收益。

**修复建议**:
- 使用 `HTTPClient` 或 Protocol 类型注解，保持与其他 manager 一致。

---

## 🟡 低优先级审查项

### 5. `types.py` 中内部请求模型未被使用

**文件**:
- `shipyard_neo/types.py:208` 之后

**建议**:
- 若短期不使用，可删减以降低维护噪音；或在调用链中真正使用这些模型统一请求体校验。

---

## 📋 后续优化大纲（建议顺序）

1. 完成 #4（`SkillManager` 参数类型收敛）。
2. 评估 #5（未使用模型删除或接入调用链）。

---

## ✅ 通过项（本轮确认）

- `Sandbox` history API 映射路径清晰，字段齐全。
- `SkillManager` 主流程接口完整（create/list/get/evaluate/promote/list/rollback）。
- 新增 SDK 测试能覆盖 history/skills 的主要 happy path。
