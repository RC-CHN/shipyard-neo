# Bay Phase 1 进度追踪

> 更新日期：2026-02-08 11:30 (UTC+8)
>
> 基于：[`phase-1.md`](phase-1.md)、[`capability-adapter-design.md`](capability-adapter-design.md)、[`idempotency-design.md`](idempotency-design.md)、[`auth-design.md`](auth-design.md)、[`profile-capability-enforcement.md`](profile-capability-enforcement.md)

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
| **Profile 能力检查** | ✅ 100% | **新增** 前置能力拦截，依赖注入实现 |
| **路径安全校验** | ✅ 100% | Bay + Ship 双层路径防护已落地 |
| **容器健康探测** | ✅ 100% | 死容器主动探测 + 自动恢复（Phase 1.5） |
| **K8s Driver** | ✅ 100% | Pod + PVC + Pod IP 直连能力已落地（Phase 2 先行项） |

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
| 7 | 运行所有测试验证 | ✅ | 已形成持续回归基线（详见第 6 节当前测试规模） |

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
| 2 | 路径安全校验 | ✅ | Bay `validate_relative_path` + Ship `resolve_path` 双层防护 |
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

### 6.1 当前规模（2026-02-08）

| 测试类型 | 数量 | 说明 |
|:--|:--|:--|
| Unit | 233 | `cd pkgs/bay && uv run pytest tests/unit --collect-only -q` |
| Integration / E2E | 140 | `cd pkgs/bay && uv run pytest tests/integration --collect-only -q` |

### 6.2 关键覆盖面

- Session 生命周期、并发 ensure_running、幂等与鉴权
- Filesystem / Upload / Download / Cargo API
- GC 任务（Idle Session / Expired Sandbox / Orphan Cargo / Orphan Container）
- Resilience 场景（Container Crash / OOM Killed / GC Race Condition）
- Docker 与 Kind(K8s) 双环境测试脚本

### 6.3 测试运行命令

```bash
# 单元测试
cd pkgs/bay && uv run pytest tests/unit -v

# E2E 测试 (docker-host 模式)
cd pkgs/bay && ./tests/scripts/docker-host/run.sh

# E2E 测试 (docker-network 模式)
cd pkgs/bay && ./tests/scripts/docker-network/run.sh

# K8s 测试 (Kind)
cd pkgs/bay && ./tests/scripts/kind/run.sh
```

## 7. 下一步行动

1. ~~运行 E2E 测试验证~~ ✅ 已完成并纳入持续回归
2. ~~删除 clients/runtime/ 目录~~ ✅ 已删除
3. ~~Idempotency-Key 接入~~ ✅ 已完成
4. ~~并发 ensure_running 竞态修复~~ ✅ 已完成
5. ~~Filesystem E2E 测试补充~~ ✅ 4 tests 已添加
6. ~~鉴权设计与实现（API Key/AuthDep）~~ ✅ 已实现（见 [`auth-design.md`](auth-design.md) 与 [`dependencies.authenticate()`](../../pkgs/bay/app/api/dependencies.py:59)）
7. ~~路径安全校验~~ ✅ 已完成（Bay + Ship 双层防护）
8. ~~容器健康探测~~ ✅ 已完成（`2667d1c`）
9. 下一步：可观测性增强（metrics / tracing）

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
[x] 路径安全校验
    ↓
[x] 容器健康探测
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

## 10. Profile 能力检查实现详情

根据 [`profile-capability-enforcement.md`](profile-capability-enforcement.md) 实现：

| # | 任务 | 状态 | 文件 |
|:--|:--|:--|:--|
| 1 | 设计文档 | ✅ | `profile-capability-enforcement.md` |
| 2 | require_capability() 工厂函数 | ✅ | `app/api/dependencies.py` |
| 3 | 能力依赖类型别名 | ✅ | `PythonCapabilityDep`, `ShellCapabilityDep`, `FilesystemCapabilityDep` |
| 4 | 更新 capabilities.py endpoints | ✅ | 所有 endpoint 使用能力依赖 |
| 5 | 单元测试 | ✅ | `tests/unit/test_capability_check.py` (6 tests) |
| 6 | E2E 测试 | ✅ | `tests/integration/test_capability_enforcement.py` (11 tests) |

### 10.1 关键设计决策

| 决策项 | 选择 |
|:--|:--|
| 检查层级 | 双层：Profile (Bay) + Runtime (Ship /meta) |
| Profile 优先级 | ✅ Profile 声明为硬约束，Runtime 为二次保障 |
| 粒度 | 粗粒度（filesystem, shell, python）Phase 1 |
| 实现方式 | FastAPI 依赖注入 |
| 错误码 | `capability_not_supported` (400) |

### 10.2 能力检查流程

```
API Request (e.g., POST /sandboxes/{id}/shell/exec)
    ↓
ShellCapabilityDep (dependency injection)
    ↓
require_capability("shell")
    ↓
sandbox_mgr.get(sandbox_id, owner)  # 获取 sandbox
    ↓
settings.get_profile(sandbox.profile_id)  # 获取 profile 配置
    ↓
if "shell" not in profile.capabilities:
    raise CapabilityNotSupportedError(400)  # ← 前置拦截，不启动容器
    ↓
return sandbox → CapabilityRouter → Ship Adapter → 容器
```

---

## 附录：关键错误类型

| 错误类 | code | status_code |
|:--|:--|:--|
| NotFoundError | `not_found` | 404 |
| CargoFileNotFoundError | `file_not_found` | 404 |
| ShipError | `ship_error` | 502 |
| SessionNotReadyError | `session_not_ready` | 503 |
| RequestTimeoutError | `timeout` | 504 |
| ValidationError | `validation_error` | 400 |
| CapabilityNotSupportedError | `capability_not_supported` | 400 |
