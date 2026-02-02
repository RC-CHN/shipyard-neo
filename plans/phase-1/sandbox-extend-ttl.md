# Sandbox TTL 延长（extend_ttl）设计说明

> 本文聚焦 `POST /v1/sandboxes/{id}/extend_ttl` 的契约、语义、幂等与错误码。
>
> 背景：Sandbox 需要支持“任务执行超预期/误配 TTL”时的续命，但必须避免语义混乱（与 keepalive/idle_timeout 混淆）与安全问题（复活已过期资源）。
>
> 关联：
> - 统一错误模型：[`plans/bay-api.md`](../bay-api.md:80)
> - Sandbox 模型：[`pkgs/bay/app/models/sandbox.py`](../pkgs/bay/app/models/sandbox.py:31)
> - 现有幂等实现：[`pkgs/bay/app/services/idempotency.py`](../pkgs/bay/app/services/idempotency.py:1)
> - 现有创建 sandbox 幂等用法：[`pkgs/bay/app/api/v1/sandboxes.py`](../pkgs/bay/app/api/v1/sandboxes.py:73)

---

## 1. 概念对齐：TTL vs idle_timeout vs keepalive

- **Sandbox TTL（`expires_at`）**：逻辑资源硬上限，到期后 Sandbox 进入 `expired`，**不可复活**。
- **Session idle_timeout（`idle_expires_at`）**：算力软回收，到期仅回收 Session（容器），Sandbox 与 Cargo 仍存在。
- **keepalive**：只延长 `idle_expires_at`，不改变 `expires_at`。
- **extend_ttl**：只延长 `expires_at`，不涉及 Session 是否启动；其效果是允许后续 `ensure_running` 继续工作。

---

## 2. 端点定义

### 2.1 路径与方法

- `POST /v1/sandboxes/{id}/extend_ttl`

### 2.2 Headers

- 可选：`Idempotency-Key: <opaque-string>`
  - 强烈建议客户端在“可能重试”的场景总是携带。

### 2.3 Request Body

```json
{
  "extend_by": 600
}
```

约束：
- `extend_by` 为正整数（单位：秒）
- Phase 1 建议限制单次上限（例如 86400），防止误用；上限值由配置决定。

---

## 3. 核心语义（已拍板）

### 3.1 不允许复活

- 若 sandbox 已过期（`expires_at < now`）则拒绝延长（`409`）。
- 目的：避免“已结束任务被重新激活”，保持资源边界与计费边界清晰。

### 3.2 不允许延长到永不过期

- **不提供**把有期限 sandbox 延长到 `ttl=null` 的能力。
- 若 sandbox 当前就是永不过期（`expires_at is null`），延长无意义，直接拒绝（`409`）。

### 3.3 服务端生成 now（防伪造）

- `now = datetime.utcnow()` 完全由服务端在处理请求时生成。
- 客户端不传 `old/expires_at`、不传 `now`。
- 目的：避免客户端通过伪造时间参与判定，从而影响“不可复活”规则。

### 3.4 计算基准：防御性 max(old, now)

设：
- `old = sandbox.expires_at`（DB 读取）
- `now = datetime.utcnow()`（服务端生成）

规则：
1. `old is null` → `409`（永不过期不能延长）
2. `old < now` → `409`（已过期不可复活）
3. 否则：

```
new = max(old, now) + extend_by
```

说明：
- 使用 `max(old, now)` 是为了抵抗边界抖动（请求处理延迟、时钟微漂移），让“延长 N 秒”的直觉更稳定。

---

## 4. 响应体与幂等

### 4.1 成功响应

- `200 OK` + 返回更新后的 `SandboxResponse`
- 返回结构与 [`get_sandbox()`](../pkgs/bay/app/api/v1/sandboxes.py:160) 一致。

示例：
```json
{
  "id": "sandbox-abc123",
  "status": "idle",
  "profile": "python-default",
  "workspace_id": "ws-xyz789",
  "capabilities": ["filesystem", "shell", "python"],
  "created_at": "2026-01-30T07:00:00Z",
  "expires_at": "2026-01-30T09:00:00Z",
  "idle_expires_at": null
}
```

选择 `200 + body` 的原因：
- 幂等键可回放同一响应（和 `POST /v1/sandboxes` 一致）
- 客户端无需额外 `GET` 即可获得新的 `expires_at`

### 4.2 幂等语义

- 支持 `Idempotency-Key`：`owner + key + path + method + body_fingerprint`
- 重试同一请求（fingerprint 相同）→ 回放同一响应
- 同 key 不同 fingerprint → `409 conflict`

说明：
- **幂等键只解决“同一请求重试”**，不解决“两个不同请求并发”。

### 4.3 并发语义（Phase 1）

- Phase 1 **接受并发丢失更新**：不同 `Idempotency-Key` 并发延长同一 sandbox，可能只生效一次。
- 后续如需严格叠加，可升级为：
  - DB 原子 update
  - sandbox 粒度串行化锁
  - 或服务端 CAS 重试

---

## 5. 错误码（已拍板）

对齐统一错误模型 [`plans/bay-api.md`](../bay-api.md:80)，使用更细的稳定 `error.code`。

### 5.1 409 sandbox_expired

- 条件：`expires_at < now`
- 含义：sandbox 已过期，拒绝延长（不可复活）

### 5.2 409 sandbox_ttl_infinite

- 条件：`expires_at is null`
- 含义：永不过期的 sandbox 延长无意义，拒绝

### 5.3 409 conflict

- 条件：幂等键冲突（同 key 不同 fingerprint）
- 含义：客户端重试/复用 key 的方式错误

错误体示例：
```json
{
  "error": {
    "code": "sandbox_expired",
    "message": "sandbox is expired, cannot extend ttl",
    "request_id": "...",
    "details": {
      "sandbox_id": "sandbox-abc123",
      "expires_at": "2026-01-30T08:00:00Z"
    }
  }
}
```

---

## 6. 需要修改/新增的代码点（面向实现，基于现状调研）

> 以下是对现有代码的具体映射，确保实现时改动范围明确。

### 6.1 API 层（新增 endpoint + 复用幂等流程）

目标文件：[`pkgs/bay/app/api/v1/sandboxes.py`](../pkgs/bay/app/api/v1/sandboxes.py:1)

现状参照：
- 幂等模式可直接复制 [`create_sandbox()`](../pkgs/bay/app/api/v1/sandboxes.py:73) 的 3 段式流程：
  1) `idempotency_svc.check()`
  2) 执行业务逻辑
  3) `idempotency_svc.save()`

需要新增：
- `ExtendTTLRequest(BaseModel)`：字段 `extend_by: int`
- `@router.post("/{sandbox_id}/extend_ttl", response_model=SandboxResponse, status_code=200)` handler
- 该 handler 需要：
  - 依赖注入：`sandbox_mgr: SandboxManagerDep`, `idempotency_svc: IdempotencyServiceDep`, `owner: AuthDep`
  - Header：`idempotency_key: str | None = Header(None, alias="Idempotency-Key")`
  - request_path：`/v1/sandboxes/{sandbox_id}/extend_ttl`

### 6.2 Manager 层（新增 `SandboxManager.extend_ttl()`）

目标文件：[`pkgs/bay/app/managers/sandbox/sandbox.py`](../pkgs/bay/app/managers/sandbox/sandbox.py:1)

现状参照：
- Sandbox owner 校验与 deleted 过滤已封装在 [`get()`](../pkgs/bay/app/managers/sandbox/sandbox.py:140)
- 现有的 lock 主要服务于 [`ensure_running()`](../pkgs/bay/app/managers/sandbox/sandbox.py:206)

建议新增方法：
- `async def extend_ttl(self, sandbox_id: str, owner: str, *, extend_by: int) -> Sandbox:`
  - 内部 `sandbox = await self.get(sandbox_id, owner)`
  - `now = datetime.utcnow()`
  - 判定/写入按本文第 3.4
  - `await self._db.commit(); await self._db.refresh(sandbox)`

### 6.3 错误类型（新增两类 409）

目标文件：[`pkgs/bay/app/errors.py`](../pkgs/bay/app/errors.py:1)

现状参照：
- 已有通用 409：[`ConflictError`](../pkgs/bay/app/errors.py:110)（用于幂等键冲突）

需要新增：
- `SandboxExpiredError(BayError)`：`code = "sandbox_expired"`, `status_code = 409`
- `SandboxTTLInfiniteError(BayError)`：`code = "sandbox_ttl_infinite"`, `status_code = 409`

### 6.4 幂等服务（无需修改，直接复用）

目标文件：[`pkgs/bay/app/services/idempotency.py`](../pkgs/bay/app/services/idempotency.py:1)

现状参照：
- `check()` 会在 fingerprint 不匹配时抛 [`ConflictError`](../pkgs/bay/app/errors.py:110)
- `save()` 的 response 序列化支持 Pydantic model（`model_dump`）

因此：
- `extend_ttl` 端点无需改动 idempotency service，只需按 create_sandbox 的方式调用。

### 6.5 文档同步（对外契约）

- 把端点补进 [`plans/bay-api.md`](../plans/bay-api.md:188) 的 6.1 Sandbox 管理章节。
- 在错误码表中补充 `sandbox_expired` / `sandbox_ttl_infinite`（对齐统一错误模型）。

---

## 7. 测试点

- 幂等：同 Idempotency-Key 重试返回同 expires_at
- 过期拒绝：expires_at < now → 409 sandbox_expired
- 永不过期拒绝：expires_at is null → 409 sandbox_ttl_infinite
- 参数校验：extend_by <= 0 → 400 validation_error
