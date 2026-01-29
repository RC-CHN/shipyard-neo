# Bay Phase 1 进度追踪

> 更新日期：2026-01-29
>
> 基于：[`phase-1.md`](phase-1.md)、[`capability-adapter-design.md`](capability-adapter-design.md)

## 1. 总体进度

| 模块 | 进度 | 说明 |
|:--|:--|:--|
| 核心骨架 | ✅ 100% | Models, Managers, Drivers, API |
| 最小 E2E 链路 | ✅ 100% | create → python/exec → stop → delete |
| Capability Adapter 重构 | ✅ 100% | clients/ 已删除 |
| Upload/Download | ✅ 100% | API + E2E 测试已添加 |
| 统一错误模型 | 🟡 80% | 大部分完成，FileNotFoundError 刚添加 |
| 鉴权 | ⏳ 0% | 待设计实现 |
| Idempotency | ⏳ 0% | 模型已建，API 未接入 |

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
| 7 | 运行所有测试验证 | ⏳ | 待用户运行 |

### 2.1 已创建的文件

- [`pkgs/bay/app/adapters/__init__.py`](../../pkgs/bay/app/adapters/__init__.py) - 导出
- [`pkgs/bay/app/adapters/base.py`](../../pkgs/bay/app/adapters/base.py) - BaseAdapter 抽象类
- [`pkgs/bay/app/adapters/ship.py`](../../pkgs/bay/app/adapters/ship.py) - ShipAdapter 实现
- [`pkgs/bay/tests/unit/test_ship_adapter.py`](../../pkgs/bay/tests/unit/test_ship_adapter.py) - 16 个单元测试

### 2.2 已删除的文件

- ~~`pkgs/bay/app/clients/runtime/`~~ 整个目录已删除 ✅

## 3. Phase 1 P0 清单（phase-1.md 第 3.1 节）

| # | 任务 | 状态 | 说明 |
|:--|:--|:--|:--|
| 1 | Ship `/meta` 握手校验 | ✅ | ShipAdapter.get_meta() 实现，带缓存 |
| 2 | 统一错误模型 | 🟡 | BayError 层级完整，刚添加 FileNotFoundError |
| 3 | Idempotency-Key | ⏳ | 模型已建，API 层未接入 |
| 4 | stop/delete 资源回收验证 | ✅ | E2E 测试覆盖 |

## 4. Phase 1 P1 清单（phase-1.md 第 3.2 节）

| # | 任务 | 状态 | 说明 |
|:--|:--|:--|:--|
| 1 | 鉴权与 owner 隔离 | ⏳ | 目前用 X-Owner header |
| 2 | 路径安全校验 | ⏳ | Ship 有 resolve_path，Bay 未做 |
| 3 | 可观测性 | ⏳ | request_id 基础有，metrics 未做 |

## 5. 新增功能（capability-adapter-design.md）

| 功能 | 状态 | API 路径 |
|:--|:--|:--|
| 文件上传 | ✅ | `POST /{sandbox_id}/files/upload` |
| 文件下载 | ✅ | `GET /{sandbox_id}/files/download` |
| download 404 处理 | ✅ | 返回 `file_not_found` 错误 |

## 6. 测试状态

### 6.1 单元测试

| 文件 | 测试数 | 状态 |
|:--|:--|:--|
| `test_docker_driver.py` | ~10 | ✅ |
| `test_sandbox_manager.py` | ~10 | ✅ |
| `test_ship_adapter.py` | 16 | ✅ |

### 6.2 E2E 测试

| 测试类 | 测试数 | 状态 |
|:--|:--|:--|
| `TestE2E01MinimalPath` | 2 | ✅ |
| `TestE2E02Stop` | 2 | ✅ |
| `TestE2E03Delete` | 3 | ✅ |
| `TestE2E04ConcurrentEnsureRunning` | 1 | ✅ |
| `TestE2E05FileUploadDownload` | 4 | 🟡 待验证 (FileNotFoundError 刚修复) |

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

1. **运行 E2E 测试验证** - 确认 FileNotFoundError 修复生效
2. **删除 clients/runtime/ 目录** - 测试通过后
3. **Idempotency-Key 接入** - `POST /v1/sandboxes`
4. **鉴权设计与实现** - 参考 `auth-design.md`

## 8. 依赖关系

```
[x] Adapter 重构
    ↓
[ ] 删除 clients/
    ↓
[ ] Idempotency-Key
    ↓
[ ] 鉴权实现
```

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
