# Phase 1 测试计划（今天可做）

> 目标：把 Phase 1 的“可回归验证”最小集合写清楚，方便你马上动手补测试。
> 
> 范围：
> - E2E（依赖本机 docker + ship 镜像）
> - Unit（尽量不依赖 docker：用 FakeDriver / stub client）

## 1. E2E（端到端）测试

### 1.1 前置条件

- `docker` 可用
- 已构建 runtime 镜像：`ship:latest`
- Bay 使用本地配置：[`pkgs/bay/config.yaml`](../pkgs/bay/config.yaml:1)
  - `connect_mode: host_port`
  - `publish_ports: true`
  - profile `runtime_port: 8123`

### 1.2 E2E-01: 最小链路（create → python/exec）

**目的**：验证 `ensure_running` + host_port 端口映射 + ship `/ipython/exec`。

**步骤**：
1. 启动 Bay（建议端口 8001 避免占用）
2. `POST /v1/sandboxes` 创建 sandbox
3. `POST /v1/sandboxes/{id}/python/exec` 执行 `print(1+2)`

**断言**：
- create 返回 201，`status=idle`，返回 `cargo_id` 非空
- python/exec 返回 200，`success=true`，`output` 含 `3`
- DB 中 sandbox `current_session_id` 非空

**注意点**：
- Ship 容器启动需要短暂时间，SessionManager 应该内部处理 readiness；如仍有偶发 502，记录日志并作为回归目标。

### 1.3 E2E-02: stop（仅回收算力）

**目的**：验证 stop 语义：销毁 session/container，但保留 sandbox/cargo。

**步骤**：
1. create + python/exec 触发 session
2. `POST /v1/sandboxes/{id}/stop`
3. 再次 `GET /v1/sandboxes/{id}`

**断言**：
- stop 返回 200
- sandbox 仍可 get，且 `status=idle`（无 current session）
- cargo volume 仍存在（通过 docker volume ls / driver_ref 验证）

### 1.4 E2E-03: delete（彻底销毁 + managed cargo 级联删除）

**目的**：验证 delete 语义：删除 sandbox + 删除所有 session/container + managed cargo 级联删除。

**步骤**：
1. create + python/exec
2. `DELETE /v1/sandboxes/{id}`
3. `GET /v1/sandboxes/{id}`

**断言**：
- delete 返回 204
- get 返回 404
- 相关 container 不存在（docker ps / inspect）
- managed cargo 对应 volume 被删除

### 1.5 E2E-04: 并发 ensure_running（同一 sandbox）

**目的**：验证并发调用不会启动多个 session（后续会加强锁/幂等）。

**步骤**：
1. create sandbox
2. 并发发起 N 个 `python/exec`（例如 5~20 个）

**断言**：
- 最终只产生 1 个 session/container（或至少不会无限增长）
- 多个请求都成功或按预期返回 `session_not_ready`（503）并可重试

> 备注：当前实现可能仍存在竞态，若发现问题先记录，后续在 Milestone 4/并发控制中修。

### 1.6 建议落地形式

- 新增脚本：`pkgs/bay/tests/integration/test_e2e_api.py`
  - 使用 `httpx` 调 Bay
  - 使用 `subprocess` / `docker` CLI 验证资源

## 2. Unit（单元测试）

### 2.1 Unit-01: SandboxManager.create

**目的**：创建 sandbox 时会创建 managed cargo，字段正确。

**做法**：
- 用 in-memory sqlite
- 用 FakeDriver（实现 create_volume/delete_volume 等，但不调用 docker）

**断言**：
- sandbox/cargo 记录存在
- cargo.managed=true 且 managed_by_sandbox_id=sandbox.id

### 2.2 Unit-02: SandboxManager.stop

**目的**：stop 会停止 session 但不删除 cargo。

**做法**：
- FakeDriver 记录 stop/destroy 调用次数
- 预先插入 session 记录并绑定 sandbox

**断言**：
- session 状态变为 stopped 或被删（取决于当前实现）
- sandbox.current_session_id 被清空
- cargo 仍存在

### 2.3 Unit-03: SandboxManager.delete

**目的**：delete 会级联删除 managed cargo。

**断言**：
- sandbox.deleted_at 不为空（tombstone）
- cargo 记录被删除
- driver.delete_volume 被调用一次

### 2.4 Unit-04: DockerDriver endpoint 解析逻辑（纯函数层面）

**目的**：保证 host_port/container_network/auto 三种模式计算 endpoint 符合预期。

**做法**：
- 不需要真实 docker，只对 `container.show()` 的返回结构做样例数据
- 建议把 endpoint 解析抽成小函数后测试

### 2.5 Unit-05: ShipClient 路径与响应解析

**目的**：确保对接 ship 的 endpoint path 与返回字段解析正确。

**做法**：
- 用 `httpx.MockTransport`

**断言**：
- `exec_python` 输出文本来自 ship `output.text`
- `list_files` 读取 ship `files`

## 3. 测试目录建议

- `pkgs/bay/tests/unit/`：纯 unit，不依赖 docker
- `pkgs/bay/tests/integration/`：依赖 docker 的 E2E/集成

## 4. TODO（后续）

- 加入 `GET /meta` 握手校验后，对应补 E2E/Unit（capabilities 校验、mount_path 校验、api_version 校验）
- 加入 IdempotencyKey 后，补 `POST /v1/sandboxes` 幂等测试
