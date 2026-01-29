# Bay Phase 1 进度追踪

> 更新日期：2026-01-29 16:15 (UTC+8)
>
> 基于：[`phase-1.md`](phase-1.md)、[`capability-adapter-design.md`](capability-adapter-design.md)、[`idempotency-design.md`](idempotency-design.md)、[`auth-design.md`](auth-design.md)

## 1. 总体进度

| 模块 | 进度 | 说明 |
|:--|:--|:--|
| 核心骨架 | ✅ 100% | Models, Managers, Drivers, API |
| 最小 E2E 链路 | ✅ 100% | create → python/exec → stop → delete |
| Capability Adapter 重构 | ✅ 100% | clients/ 已删除，adapters/ 已创建 |
| Upload/Download | ✅ 100% | API + E2E 测试已添加 |
| Filesystem (read/write/list/delete) | ✅ 100% | API + 单元测试 + E2E 测试完整 |
| 统一错误模型 | ✅ 100% | BayError 层级完整 |
| Idempotency | ✅ 100% | Service + API 已接入，E2E 测试通过 |
| 并发竞态修复 | ✅ 100% | ensure_running 加锁 + 双重检查 |
| 鉴权 | ✅ 100% | API Key 认证 + `AuthDep` 注入，支持 dev 模式 `X-Owner` |

## 2. Capability Adapter 重构详情

根据 [`capability-adapter-design.md`](capability-adapter-design.md) 的迁移步骤：

| # | 任务 | 状态 | 文件 |
|:--|:--|:--|:--|
| 1 | 创建 `adapters/` 目录和文件 | ✅ | `adapters/{__init__.py, base.py, ship.py}` |
| 2 | 修改 CapabilityRouter 使用 Adapter | ✅ | `router/capability/capability.py` |
| 3 | 删除 `clients/runtime/` 目录 | ✅ | 已删除 |
| 4 | 添加 upload/download API | ✅ | `api/v1/capabilities.py` |
| 5 | 更新 config（ipython → python） | ✅ | `config.py`, `config.yaml.example` |
| 6 | 更新/重命名测试文件 | ✅ | `tests/unit/test_ship_adapter.py` |
| 7 | 运行所有测试验证 | ✅ | 20 E2E passed (2026-01-29 11:33) |

### 2.1 已创建的文件

- [`pkgs/bay/app/adapters/__init__.py`](../../pkgs/bay/app/adapters/__init__.py) - 导出
- [`pkgs/bay/app/adapters/base.py`](../../pkgs/bay/app/adapters/base.py) - BaseAdapter 抽象类
- [`pkgs/bay/app/adapters/ship.py`](../../pkgs/bay/app/adapters/ship.py) - ShipAdapter 实现
- [`pkgs/bay/tests/unit/test_ship_adapter.py`](../../pkgs/bay/tests/unit/test_ship_adapter.py) - 21 个单元测试（含 write_file, delete_file）

### 2.2 已删除的文件

- ~~`pkgs/bay/app/clients/runtime/`~~ 整个目录已删除 ✅

## 3. Phase 1 P0 清单（phase-1.md 第 3.1 节）

| # | 任务 | 状态 | 说明 |
|:--|:--|:--|:--|
| 1 | Ship `/meta` 握手校验 | ✅ | ShipAdapter.get_meta() 实现，带缓存 |
| 2 | 统一错误模型 | ✅ | BayError 层级完整，ConflictError 用于幂等冲突 |
| 3 | Idempotency-Key | ✅ | IdempotencyService + API 已接入，E2E 测试通过 |
| 4 | stop/delete 资源回收验证 | ✅ | E2E 测试覆盖 |

## 4. Phase 1 P1 清单（phase-1.md 第 3.2 节）

| # | 任务 | 状态 | 说明 |
|:--|:--|:--|:--|
| 1 | 鉴权与 owner 隔离 | ✅ | API Key 认证（可关闭匿名）；`X-Owner` 仅在 `allow_anonymous=true` 时用于开发测试 |
| 2 | 路径安全校验 | ⏳ | Ship 有 resolve_path，Bay 未做 |
| 3 | 可观测性 | ⏳ | request_id 基础有，metrics 未做 |

## 5. 新增功能（capability-adapter-design.md）

| 功能 | 状态 | API 路径 |
|:--|:--|:--|
| 文件上传 | ✅ | `POST /{sandbox_id}/files/upload` |
| 文件下载 | ✅ | `GET /{sandbox_id}/files/download` |
| download 404 处理 | ✅ | 返回 `file_not_found` 错误 |
| 文件读取 | ✅ | `POST /{sandbox_id}/files/read` |
| 文件写入 | ✅ | `POST /{sandbox_id}/files/write` |
| 目录列表 | ✅ | `POST /{sandbox_id}/files/list` |
| 文件删除 | ✅ | `POST /{sandbox_id}/files/delete` |

## 6. 测试状态

### 6.1 单元测试（91 tests）

| 文件 | 测试数 | 状态 |
|:--|:--|:--|
| `test_auth.py` | 18 | ✅ （API Key / allow_anonymous / X-Owner 行为） |
| `test_docker_driver.py` | 12 | ✅ |
| `test_sandbox_manager.py` | 12 | ✅ |
| `test_ship_adapter.py` | 21 | ✅ （含 write_file, delete_file） |
| `test_idempotency.py` | 24 | ✅ |

### 6.2 E2E 测试（23 tests）

| 测试类 | 测试数 | 状态 |
|:--|:--|:--|
| `TestE2E00Auth` | 3 | ✅ （缺失/错误/正确 API Key） |
| `TestE2E01MinimalPath` | 2 | ✅ |
| `TestE2E02Stop` | 2 | ✅ |
| `TestE2E03Delete` | 3 | ✅ |
| `TestE2E04ConcurrentEnsureRunning` | 1 | ✅ |
| `TestE2E05FileUploadDownload` | 4 | ✅ |
| `TestE2E06Filesystem` | 4 | ✅ **新增** read/write/list/delete |
| `TestE2E07Idempotency` | 4 | ✅ |

### 6.3 测试运行命令

```bash
# 单元测试
cd pkgs/bay && uv run pytest tests/unit -v

# E2E 测试 (docker-host 模式)
cd pkgs/bay && ./tests/scripts/docker-host/run.sh

# E2E 测试 (docker-network 模式)
cd pkgs/bay && ./tests/scripts/docker-network/run.sh
```

## 7. 下一步行动

1. ~~运行 E2E 测试验证~~ ✅ 20 passed (2026-01-29 11:33)
2. ~~删除 clients/runtime/ 目录~~ ✅ 已删除
3. ~~Idempotency-Key 接入~~ ✅ 已完成
4. ~~并发 ensure_running 竞态修复~~ ✅ 已完成
5. ~~Filesystem E2E 测试补充~~ ✅ 4 tests 已添加
6. ~~鉴权设计与实现（API Key/AuthDep）~~ ✅ 已实现（见 [`auth-design.md`](auth-design.md) 与 [`dependencies.authenticate()`](../../pkgs/bay/app/api/dependencies.py:59)）
7. **路径安全校验** - 参考 [`auth-design.md`](auth-design.md)

## 8. 依赖关系

```
[x] Adapter 重构
    ↓
[x] 删除 clients/
    ↓
[x] Idempotency-Key
    ↓
[x] Filesystem 测试补充
    ↓
[x] 鉴权实现（API Key/AuthDep）
    ↓
[ ] 路径安全校验
```

## 9. Idempotency 实现详情

根据 [`idempotency-design.md`](idempotency-design.md) 实现：

| # | 任务 | 状态 | 文件 |
|:--|:--|:--|:--|
| 1 | 设计文档 | ✅ | `idempotency-design.md` |
| 2 | IdempotencyService | ✅ | `app/services/idempotency.py` |
| 3 | 配置项 | ✅ | `app/config.py` (IdempotencyConfig) |
| 4 | 依赖注入 | ✅ | `app/api/dependencies.py` |
| 5 | API 接入 | ✅ | `app/api/v1/sandboxes.py` |
| 6 | 单元测试 | ✅ | `tests/unit/test_idempotency.py` (24 tests) |
| 7 | E2E 测试 | ✅ | `tests/integration/test_e2e_api.py` (4 tests) |

### 9.1 关键设计决策

| 决策项 | 选择 |
|:--|:--|
| fingerprint 包含 body | ✅ 包含 (SHA256 hash) |
| 409 返回原响应 | ❌ 仅返回错误 |
| TTL | 1 小时 (可配置) |
| 存储 | SQLite 同库 |
| 过期清理 | 惰性删除 |

---

## 附录：关键错误类型

| 错误类 | code | status_code |
|:--|:--|:--|
| NotFoundError | `not_found` | 404 |
| FileNotFoundError | `file_not_found` | 404 |
| ShipError | `ship_error` | 502 |
| SessionNotReadyError | `session_not_ready` | 503 |
| TimeoutError | `timeout` | 504 |
| ValidationError | `validation_error` | 400 |
