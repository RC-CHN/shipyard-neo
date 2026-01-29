# Bay Phase 1 当前进展（截至 2026-01-29）

> 本文记录 Bay Phase 1 的开发进展、已跑通的最小链路、以及后续必须补齐的工作项。
>
> 更新日期：2026-01-29 11:33 (UTC+8)
>
> 相关设计：
> - [`plans/bay-implementation-path.md`](plans/bay-implementation-path.md:1)
> - [`plans/bay-api.md`](plans/bay-api.md:1)
> - [`plans/bay-design.md`](plans/bay-design.md:1)
> - [`progress.md`](progress.md) - 详细进度追踪

## 0. 总体进度摘要

| 模块 | 进度 | 说明 |
|:--|:--|:--|
| 核心骨架 | ✅ 100% | Models, Managers, Drivers, API |
| 最小 E2E 链路 | ✅ 100% | create → python/exec → stop → delete |
| Capability Adapter 重构 | ✅ 100% | clients/ 已删除，adapters/ 已创建 |
| Upload/Download | ✅ 100% | API + E2E 测试已添加 |
| Filesystem (read/write/list/delete) | ✅ 100% | API 完整 + E2E 测试已添加 |
| 统一错误模型 | ✅ 100% | BayError 层级完整 |
| Idempotency | ✅ 100% | Service + API 已接入，E2E 测试通过 |
| 并发竞态修复 | ✅ 100% | ensure_running 加锁 + 双重检查 |
| 鉴权 | ⏳ 0% | 框架已预留，待实现 JWT 验证 |

## 1. 已达成的里程碑

### 1.1 Bay 工程骨架与核心分层已落地

已落地目录：[`pkgs/bay/`](pkgs/bay:1)

- FastAPI 入口：[`pkgs/bay/app/main.py`](pkgs/bay/app/main.py:1)
- 配置系统（支持 YAML + env 覆盖）：[`pkgs/bay/app/config.py`](pkgs/bay/app/config.py:1)
- DB（SQLite async）：[`pkgs/bay/app/db/session.py`](pkgs/bay/app/db/session.py:1)
- Models：
  - Sandbox：[`pkgs/bay/app/models/sandbox.py`](pkgs/bay/app/models/sandbox.py:1)
  - Workspace：[`pkgs/bay/app/models/workspace.py`](pkgs/bay/app/models/workspace.py:1)
  - Session：[`pkgs/bay/app/models/session.py`](pkgs/bay/app/models/session.py:1)
  - IdempotencyKey：[`pkgs/bay/app/models/idempotency.py`](pkgs/bay/app/models/idempotency.py:1)
- Driver 抽象 + DockerDriver：
  - Driver 接口：[`pkgs/bay/app/drivers/base.py`](pkgs/bay/app/drivers/base.py:1)
  - DockerDriver（支持 container_network/host_port/auto）：[`pkgs/bay/app/drivers/docker/docker.py`](pkgs/bay/app/drivers/docker/docker.py:1)
- Managers：
  - SandboxManager：[`pkgs/bay/app/managers/sandbox/sandbox.py`](pkgs/bay/app/managers/sandbox/sandbox.py:1)
  - SessionManager：[`pkgs/bay/app/managers/session/session.py`](pkgs/bay/app/managers/session/session.py:1)
  - WorkspaceManager：[`pkgs/bay/app/managers/workspace/workspace.py`](pkgs/bay/app/managers/workspace/workspace.py:1)
- Adapters（重构后）：
  - BaseAdapter：[`pkgs/bay/app/adapters/base.py`](pkgs/bay/app/adapters/base.py:1)
  - ShipAdapter：[`pkgs/bay/app/adapters/ship.py`](pkgs/bay/app/adapters/ship.py:1)
- CapabilityRouter：[`pkgs/bay/app/router/capability/capability.py`](pkgs/bay/app/router/capability/capability.py:1)
- Services：
  - IdempotencyService：[`pkgs/bay/app/services/idempotency.py`](pkgs/bay/app/services/idempotency.py:1)

### 1.2 Phase 1 最小 E2E 链路已跑通

已跑通链路（符合 [`plans/bay-implementation-path.md`](plans/bay-implementation-path.md:19) 的 0.2 验收思路）：

1. `POST /v1/sandboxes` 创建 sandbox（lazy session，初始 `status=idle`）
2. `POST /v1/sandboxes/{id}/python/exec`
   - Bay 触发 `ensure_running`
   - DockerDriver 创建并启动 ship 容器 + workspace volume 挂载
   - 通过 **host_port 端口映射** 获取 endpoint（`http://127.0.0.1:<HostPort>`）
   - 调用 ship `/ipython/exec` 成功返回

对应 API 入口：
- sandboxes：[`pkgs/bay/app/api/v1/sandboxes.py`](pkgs/bay/app/api/v1/sandboxes.py:1)
- capabilities：[`pkgs/bay/app/api/v1/capabilities.py`](pkgs/bay/app/api/v1/capabilities.py:1)

### 1.3 修复：首个 python/exec 请求不再需要客户端重试

现状：首个 `python/exec` 单次请求即可返回 200。
原因：在 Session 启动后增加了 runtime readiness 等待（容器启动 ≠ HTTP server ready）。

### 1.4 并发 ensure_running 竞态已修复

问题：多个并发请求导致创建多个 session/容器。
修复：在 `SandboxManager.ensure_running()` 中实现 asyncio.Lock + 双重检查。
详见 [`test-report.md`](test-report.md:163)。

### 1.5 Idempotency-Key 已完整实现

- IdempotencyService 实现：[`pkgs/bay/app/services/idempotency.py`](pkgs/bay/app/services/idempotency.py:1)
- API 接入：`POST /v1/sandboxes` 支持 `Idempotency-Key` header
- 配置项：TTL 1小时（可配置）
- 测试：24 单元测试 + 4 E2E 测试
- 详见 [`idempotency-design.md`](idempotency-design.md)

### 1.6 Ship 镜像本地已构建

- 已执行 `docker build -t ship:latest pkgs/ship`，可用于 Bay 直接拉起 runtime。

## 2. 当前可用的接口清单

### 2.1 Bay 自身
- `GET /health`
- `GET /v1/profiles`

### 2.2 Sandboxes（已可用）
- `POST /v1/sandboxes`
- `GET /v1/sandboxes`
- `GET /v1/sandboxes/{id}`
- `POST /v1/sandboxes/{id}/keepalive`
- `POST /v1/sandboxes/{id}/stop`
- `DELETE /v1/sandboxes/{id}`

### 2.3 Capabilities（已可用，E2E 测试覆盖完整）
- `POST /v1/sandboxes/{id}/python/exec`
- `POST /v1/sandboxes/{id}/shell/exec`
- `POST /v1/sandboxes/{id}/files/read`
- `POST /v1/sandboxes/{id}/files/write`
- `POST /v1/sandboxes/{id}/files/list`
- `POST /v1/sandboxes/{id}/files/delete`
- `POST /v1/sandboxes/{id}/files/upload`
- `GET /v1/sandboxes/{id}/files/download`

## 3. 当前运行默认配置（dev）

- [`pkgs/bay/config.yaml`](pkgs/bay/config.yaml:1)
  - 默认按“Bay 跑宿主机”方式连接：`connect_mode=host_port` + `publish_ports=true`
  - profile：仅保留 `python-default`
  - ship runtime_port：8123（与 ship 镜像启动日志一致）

## 4. P0 清单完成情况

> Phase 1 核心功能已全部完成。

| # | 任务 | 状态 | 说明 |
|:--|:--|:--|:--|
| 1 | Ship `/meta` 握手校验 | ✅ | ShipAdapter.get_meta() 实现，带缓存 |
| 2 | 统一错误模型 | ✅ | BayError 层级完整，ConflictError 用于幂等冲突 |
| 3 | Idempotency-Key | ✅ | IdempotencyService + API 已接入 |
| 4 | stop/delete 资源回收 | ✅ | E2E 测试覆盖 |
| 5 | 并发 ensure_running | ✅ | asyncio.Lock + 双重检查 |

## 5. P1 清单（建议完成）

| # | 任务 | 状态 | 说明 |
|:--|:--|:--|:--|
| 1 | JWT Token 验证 | ⏳ | 框架预留，待实现 |
| 2 | 路径安全校验 | ⏳ | Ship 有 resolve_path，Bay 侧待实现 |
| 3 | 可观测性增强 | ⏳ | request_id 基础有，metrics 未做 |

详见 [`auth-design.md`](auth-design.md) 了解鉴权设计与实现计划。

## 6. Phase 1 明确不做

- K8sDriver（Phase 2）
- 对外暴露 Session
- 多 sandbox 共享 workspace

## 7. 下一步执行顺序

1. ~~接入 ship `GET /meta` 握手~~ ✅
2. ~~把 `Idempotency-Key` 接入 `POST /v1/sandboxes`~~ ✅
3. ~~写 E2E 脚本并校验资源回收~~ ✅
4. ~~并发 ensure_running 竞态修复~~ ✅
5. **JWT Token 验证实现** - 参考 [`auth-design.md`](auth-design.md:277)
6. **路径安全校验** - 参考 [`auth-design.md`](auth-design.md:373)

## 8. 测试覆盖

### 8.1 单元测试（69 tests）

| 文件 | 测试数 | 说明 |
|:--|:--|:--|
| `test_docker_driver.py` | 12 | DockerDriver endpoint 解析 |
| `test_sandbox_manager.py` | 12 | SandboxManager 生命周期 |
| `test_ship_adapter.py` | 21 | ShipAdapter HTTP 请求/响应（含 write_file, delete_file） |
| `test_idempotency.py` | 24 | IdempotencyService 完整测试 |

### 8.2 E2E 测试（20 tests）

| 测试类 | 测试数 | 说明 |
|:--|:--|:--|
| `TestE2E01MinimalPath` | 2 | 最小链路 (create → exec) |
| `TestE2E02Stop` | 2 | stop 语义（回收算力） |
| `TestE2E03Delete` | 3 | delete 语义（彻底销毁） |
| `TestE2E04ConcurrentEnsureRunning` | 1 | 并发 ensure_running |
| `TestE2E05FileUploadDownload` | 4 | 文件上传下载 |
| `TestE2E06Filesystem` | 4 | **新增** read/write/list/delete E2E |
| `TestE2E07Idempotency` | 4 | 幂等键测试 |

### 8.3 运行命令

```bash
# 单元测试
cd pkgs/bay && uv run pytest tests/unit -v

# E2E 测试 (docker-host 模式)
cd pkgs/bay && ./tests/scripts/docker-host/run.sh
```
