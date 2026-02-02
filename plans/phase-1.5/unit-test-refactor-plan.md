# Unit 测试补强与重构建议

> 状态：拟定
> 更新时间：2026-02-02

## 1. 目标

- 找出 unit 测试的**缺口**（未覆盖的边界与行为）。
- 标记**可拆分/可合并**的测试点，降低重复与维护成本。
- 保持与当前架构一致（FakeDriver / In-memory DB / MockTransport）。

---

## 2. 现状覆盖简述

- 认证：[`pkgs/bay/tests/unit/test_auth.py`](pkgs/bay/tests/unit/test_auth.py:1)
- 能力检查：[`pkgs/bay/tests/unit/test_capability_check.py`](pkgs/bay/tests/unit/test_capability_check.py:1)
- Router 能力验证：[`pkgs/bay/tests/unit/test_capability_router.py`](pkgs/bay/tests/unit/test_capability_router.py:1)
- Docker driver 解析：[`pkgs/bay/tests/unit/test_docker_driver.py`](pkgs/bay/tests/unit/test_docker_driver.py:1)
- GC Scheduler/Task：[`pkgs/bay/tests/unit/test_gc_scheduler.py`](pkgs/bay/tests/unit/test_gc_scheduler.py:1) / [`test_gc_tasks.py`](pkgs/bay/tests/unit/test_gc_tasks.py:1)
- 幂等：[`pkgs/bay/tests/unit/test_idempotency.py`](pkgs/bay/tests/unit/test_idempotency.py:1)
- 路径校验：[`pkgs/bay/tests/unit/test_path_validator.py`](pkgs/bay/tests/unit/test_path_validator.py:1)
- Sandbox manager：[`pkgs/bay/tests/unit/test_sandbox_manager.py`](pkgs/bay/tests/unit/test_sandbox_manager.py:1)
- Ship adapter：[`pkgs/bay/tests/unit/test_ship_adapter.py`](pkgs/bay/tests/unit/test_ship_adapter.py:1)

---

## 3. 可补强的测试点（建议）

### 3.1 认证模块
- **缺口**：`X-Owner` 与 `Authorization` 同时存在时的优先级（匿名模式下是否忽略/覆盖）。
- **缺口**：`Authorization` 前后空格、大小写混合（`Bearer`）的处理边界。

建议新增：
- `test_x_owner_and_valid_bearer_priority`
- `test_bearer_with_extra_spaces`

---

### 3.2 Capability 依赖与 Router
- **缺口**：`require_capability()` 在 sandbox not found（`sandbox_mgr.get` 返回 None）时的错误路径。
- **缺口**：`CapabilityRouter._require_capability()` 在 adapter `get_meta()` 抛异常时的错误包装（若有）。

建议新增：
- `test_require_capability_sandbox_missing`
- `test_require_capability_meta_error_propagates`

---

### 3.3 DockerDriver 解析
- **缺口**：`_resolve_host_port` 遇到 `HostPort` 非数字/空字符串的处理。
- **缺口**：`_resolve_container_ip` 网络字段缺失/None 的异常路径。

建议新增：
- `test_resolve_host_port_invalid_port_value`
- `test_resolve_container_ip_missing_networks`

---

### 3.4 GC Scheduler
- **缺口**：`run_once` 在 `_run_lock` 已被持有时的行为（若实现允许并发调用）。
- **缺口**：`start()` 与 `stop()` 在重复调用下的极端时序（`stop` before `start`）。

建议新增：
- `test_run_once_lock_reentry`
- `test_stop_without_start`

---

### 3.5 GC Tasks
- **缺口**：`IdleSessionGC._process_sandbox` 结果为 False 的 skip 计数逻辑（当前只覆盖异常）。
- **缺口**：`ExpiredSandboxGC` 删除级联失败时的错误计数与继续行为。
- **缺口**：`OrphanCargoGC` `_find_orphans` 返回空列表时计数。

建议新增：
- `test_idle_session_process_returns_false_counts_skipped`
- `test_expired_sandbox_delete_error_collects`
- `test_orphan_workspace_no_orphans`

---

### 3.6 IdempotencyService
- **缺口**：`save()` 后直接查询 DB 记录字段（`status_code/response_snapshot/expires_at`）一致性。
- **缺口**：`check()` 对于过期记录的删除行为是否提交（flush/commit）覆盖。

建议新增：
- `test_save_persists_status_code`
- `test_check_expired_deletes_record`

---

### 3.7 Path Validator
- **缺口**：`validate_relative_path` 对 `"."` 的路径归一化后是否一致（已有 `test_current_dir_only`，可补“含空段”的边界）。
- **缺口**：多级空段 `a//b` 是否允许/规范化。

建议新增：
- `test_normalizes_double_slash`

---

### 3.8 SandboxManager
- **缺口**：`ensure_running`（如果有）应覆盖：已有 session 复用 / session 状态不一致 / driver 启动失败。
- **缺口**：`stop()` 对没有 session 的 sandbox 是否稳态（当前覆盖有限）。
- **缺口**：`delete()` 对 unmanaged cargo 的处理（如果支持）。

建议新增：
- `test_stop_without_session`
- `test_delete_unmanaged_workspace_not_deleted`
- `test_ensure_running_reuses_active_session`（如存在）

---

### 3.9 ShipAdapter
- **缺口**：`exec_python`/`exec_shell` 解析异常 JSON/非 JSON 错误响应（HTTP 非 200）。
- **缺口**：`upload/download` 参数名一致性（`file_path` vs `path`）的验证。

建议新增：
- `test_exec_python_non_json_error_response`
- `test_exec_shell_http_error_propagates`

---

## 4. 可拆分/重构建议（不改逻辑，仅结构）

### 4.1 目录拆分（可选）
```
pkgs/bay/tests/unit/
  auth/
  capability/
  drivers/
  gc/
  idempotency/
  path/
  managers/
  adapters/
```

### 4.2 去重建议
- `test_capability_check.py` 与 `test_capability_router.py` 都做 “capability missing” 断言，**可以保留但减少重复细节断言**，一个测试验证 message，一个测试验证 details。
- `test_ship_adapter.py` 里大量重复“捕获 request path + body”，可抽公共 helper。

---

## 5. 结论

Unit 测试整体覆盖较全面，但仍存在若干“错误路径/边界/非 happy-path”的缺口。建议优先补强 3.1~3.6 的关键逻辑缺口，再考虑目录拆分与重复消减。