# Bay Phase 1 编码实现路径（执行计划）

> 目标：把讨论结论收敛成一条可执行的实现路径，便于按阶段落地与验收。
>
> 相关背景文档：
> - 架构与概念：[plans/bay-design.md](plans/bay-design.md:1)
> - API 契约（v1）：[plans/bay-api.md](plans/bay-api.md:1)
> - 概念与职责边界：[plans/bay-concepts.md](plans/bay-concepts.md:1)

## 0. Phase 1 目标与验收标准

### 0.1 MVP 目标（必须）

- 以 `Sandbox` 为唯一对外句柄（稳定 `sandbox_id`），对外不暴露 session_id。
- Bay 能通过 DockerDriver 启动 Ship 容器并挂载 cargo 到固定路径 `/workspace`。
- Bay 支持 `/v1/sandboxes` 创建、查询、列表，支持 `keepalive`、`stop`（只回收算力）、`delete`（彻底销毁）。
- Bay 能路由至少 1 条能力链路（建议 `python/exec`）并在启动后通过 Ship `GET /meta` 做运行时契约握手校验。

### 0.2 验收用最小链路（E2E）

1. `POST /v1/sandboxes`（lazy session）
2. `POST /v1/sandboxes/{id}/python/exec`（触发 ensure_running → 创建容器 → 调 Ship `GET /meta` → 执行）
3. `POST /v1/sandboxes/{id}/stop`（停掉该 sandbox 下全部运行实例，保留 cargo）
4. `DELETE /v1/sandboxes/{id}`（彻底销毁；managed cargo 级联删除）

## 1. Phase 1 前置决策（已拍板）

### 1.1 运行环境与 driver 选择

Phase 1 需要覆盖 3 种运行状况：

- 运行状况 A：Bay 在容器里运行，通过挂载 docker.sock 控制宿主 Docker
- 运行状况 B：Bay 运行在宿主机上，直接使用宿主 docker.sock
- 运行状况 C：Bay 运行在 Kubernetes 里，不挂载 docker.sock，改用 K8sDriver（Phase 2 重点）

Phase 1 默认实现 DockerDriver（A/B）；K8sDriver 仅保留接口占位。

### 1.2 存储后端与挂载路径

- Cargo 后端：Docker Volume
- 容器内挂载路径：固定为 `/workspace`
- API 文件路径：必须是相对路径（相对 `/workspace`），拒绝绝对路径与 `../`

对齐约束见：[plans/bay-design.md](plans/bay-design.md:43) 与 [plans/bay-api.md](plans/bay-api.md:281)。

### 1.3 配额策略

- Phase 1 采用“软配额”：
  - 在写入/上传时基于 `size_limit_mb` 做拦截
  - 定期统计 cargo 目录/volume 用量
- Phase 1 不做底层硬盘配额（Docker Volume 不依赖底层 FS quota）

### 1.4 元数据与无状态化

- Phase 1 默认 DB：SQLite（单机单实例 Bay）
- 未来扩展到多实例：切换到 Postgres/MySQL + DB 行锁/乐观锁实现按 sandbox 粒度串行化，不引入 etcd/redis
- DB/ORM：SQLModel（SQLAlchemy）+ Alembic

## 2. Bay 工程结构（建议落地在 pkgs/bay/）

> 目录形态参考：[plans/bay-design.md](plans/bay-design.md:301)，并结合 Phase 1 需求裁剪。

建议结构：

- `pkgs/bay/pyproject.toml`
- `pkgs/bay/bay/main.py`：FastAPI 入口（/v1 路由注册）
- `pkgs/bay/bay/config.py`：配置（DB DSN、driver type、docker sock、默认镜像等）
- `pkgs/bay/bay/db/`
  - `session.py`：SQLModel engine/session 管理
  - `migrations/`：Alembic
- `pkgs/bay/bay/models/`：SQLModel 实体（tables）
- `pkgs/bay/bay/drivers/`：DockerDriver（Phase1）+ K8sDriver 占位
- `pkgs/bay/bay/managers/`：CargoManager / SessionManager / SandboxManager
- `pkgs/bay/bay/clients/`：RuntimeClient 抽象 + ShipClient
- `pkgs/bay/bay/router/`：CapabilityRouter（策略+路由）
- `pkgs/bay/bay/api/`：FastAPI 路由模块（sandbox/cargo/admin）
- `pkgs/bay/tests/`：unit + integration

## 3. 数据模型与表设计（Phase 1 必要字段）

> 目标：满足幂等、并发、重启恢复、tombstone，以及 stop/delete 语义。

### 3.1 sandboxes（对外资源）

- `id`（sandbox_id）
- `owner`
- `profile_id`
- `cargo_id`
- `current_session_ids`（Phase 1 可先用单个 current_session_id；但 stop 语义按“所有运行实例”定义）
- `expires_at`（TTL；允许 null/0 表示不过期）
- `deleted_at`（tombstone：对外 404，但内部用于审计/判定）
- `version`（乐观锁）

### 3.2 cargos

- `id`
- `owner`
- `backend`（例如 docker_volume）
- `driver_ref`（volume name）
- `managed` + `managed_by_sandbox_id`
- `size_limit_mb`
- `created_at/last_accessed_at`

### 3.3 sessions（运行实例）

- `id`
- `sandbox_id`
- `runtime_type`（Phase 1 = ship）
- `container_id`
- `endpoint`（容器内 Ship 访问地址，或由网络决定）
- `desired_state/observed_state/last_observed_at`
- `created_at/last_active_at`

### 3.4 idempotency_keys

- `owner` + `key`
- `request_fingerprint`
- `response_snapshot` + `status_code`
- `expires_at`

规则参见：[plans/bay-api.md](plans/bay-api.md:41)。

## 4. Driver/Manager/Router 关键实现要点

### 4.1 DockerDriver（Phase 1）

职责：只做容器生命周期与状态，不承载业务策略（见：[plans/bay-design.md](plans/bay-design.md:183)）。

最小能力：
- create: 组装容器 spec（镜像、env、资源、network、labels）并创建容器
- start: 启动容器并返回 endpoint（或由 Manager 决定如何访问）
- stop/destroy/status/logs

必须保证：
- cargo volume 挂载到 `/workspace`
- 所有资源带 label：owner/sandbox_id/session_id/cargo_id/profile_id

### 4.2 SessionManager

关键：`ensure_running(sandbox_id)`

- 获取 sandbox 行锁（或 version CAS）
- 若已有可用 session → 返回
- 否则创建 session 记录 → 调 DockerDriver 创建/启动 → 更新 endpoint
- 启动后调用 Ship `GET /meta` 做握手校验：
  - 校验 `cargo.mount_path == /workspace`
  - 校验 capabilities ⊇ profile.capabilities
  - 校验 `api_version`

Ship 已支持 `GET /meta`：[`pkgs/ship/app/main.py`](pkgs/ship/app/main.py:62)。

### 4.3 CargoManager

- create: 创建 docker volume，落库，标记 managed/external
- delete: 若 managed 且 sandbox deleted_at 非空才允许直接删（或返回 409/403，需按 API 文档统一）
- 软配额：写入/上传前检查 + 定期统计（Phase 1 先做接口与字段，统计可先简化）

### 4.4 CapabilityRouter + ShipClient

- Router：承载策略（超时/重试/熔断/审计/指标/限流）+ 路由（sandbox_id → session endpoint）
- ShipClient：纯 HTTP 客户端（序列化/反序列化、基础错误映射）

分层见：[plans/bay-design.md](plans/bay-design.md:288)。

## 5. REST API 实现顺序（建议）

以 [plans/bay-api.md](plans/bay-api.md:188) 为准，Phase 1 建议按如下顺序实现：

1. `/v1/health`（Bay 自身）
2. `/v1/profiles`（静态配置返回）
3. `/v1/sandboxes`：create/get/list
4. `/v1/sandboxes/{id}/python/exec`（打通 ensure_running + ship meta handshake + exec）
5. `/v1/sandboxes/{id}/stop`
6. `/v1/sandboxes/{id}` delete
7. Files/Shell 相关端点逐步补全
8. Cargo API（高级/管理面）用 feature flag 控制对外暴露

## 6. 测试策略

- Unit
  - models：SQLModel schema 与迁移
  - managers：ensure_running 幂等（并发）/ stop/delete 语义
  - router：错误映射一致性（对齐 [plans/bay-api.md](plans/bay-api.md:80)）

- Integration
  - 依赖本机 docker：启动 ship 容器，跑完整链路（见 0.2）

## 7. 里程碑拆分（不估时，只定义交付物）

- Milestone 1：Bay 工程骨架 + DB/迁移 + profile 静态配置加载
- Milestone 2：DockerDriver + CargoManager（volume）+ SessionManager.ensure_running
- Milestone 3：Sandbox API（create/get/list/stop/delete）+ /python/exec 最小链路
- Milestone 4：Ship `GET /meta` 握手校验接入（已在 Ship 侧落地），完善错误模型/幂等键
- Milestone 5：扩展到 filesystem/shell 能力 + cargo 管理面（可选）

---

## 附：当前已落地的前置能力

- Ship 已新增 `GET /meta` 运行时自描述接口，用于 Bay 握手与能力校验：[`pkgs/ship/app/main.py`](pkgs/ship/app/main.py:62)
