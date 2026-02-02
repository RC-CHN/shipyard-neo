# Phase 1 测试执行报告

> 执行日期：2026-01-28
> 
> 本文档记录 Phase 1 测试计划的执行结果，包括模拟的用户操作、发现的问题及修复情况。

## 1. 测试概览

| 类别 | 测试数量 | 通过 | 失败 |
|:--|:--:|:--:|:--:|
| 单元测试 | 36 | 36 | 0 |
| E2E 测试 | 8 | 8 | 0 |
| **总计** | **44** | **44** | **0** |

### 1.1 单元测试分布

| 测试文件 | 测试数量 | 说明 |
|:--|:--:|:--|
| `test_sandbox_manager.py` | 12 | SandboxManager 生命周期测试 |
| `test_docker_driver.py` | 12 | DockerDriver endpoint 解析逻辑 |
| `test_ship_client.py` | 12 | ShipClient HTTP 请求/响应解析 |

### 1.2 E2E 测试分布

| 测试类 | 测试数量 | 说明 |
|:--|:--:|:--|
| `TestE2E01MinimalPath` | 2 | 最小链路 (create → exec) |
| `TestE2E02Stop` | 2 | stop 语义（回收算力） |
| `TestE2E03Delete` | 3 | delete 语义（彻底销毁） |
| `TestE2E04ConcurrentEnsureRunning` | 1 | 并发 ensure_running |

## 2. E2E 测试模拟的用户操作

### 2.1 E2E-01: 最小链路

**模拟场景**：用户首次使用 - 创建沙箱并执行 Python 代码

**用户操作流程**：
```
1. 调用 POST /v1/sandboxes 创建沙箱
2. 调用 POST /v1/sandboxes/{id}/python/exec 执行 print(1+2)
3. 查看返回结果
```

**验证点**：
- 创建返回 201，status 为 `idle`（懒启动，尚未创建容器）
- 执行代码时触发 `ensure_running`，自动启动 Ship 容器
- 执行结果 success=true，output 包含 "3"
- sandbox 状态变为 `ready`

**实际日志片段**：
```
sandbox.create sandbox_id=sandbox-0af541514707
session.create session_id=sess-a1ec5763a384
docker.start endpoint=http://127.0.0.1:42468
session.runtime_ready attempts=2 elapsed_ms=517
capability.python.exec code_len=10
POST /v1/sandboxes/sandbox-0af541514707/python/exec 200 OK
```

### 2.2 E2E-02: Stop（回收算力）

**模拟场景**：用户暂时不用沙箱，想释放计算资源但保留数据

**用户操作流程**：
```
1. 创建沙箱并执行代码（触发容器启动）
2. 调用 POST /v1/sandboxes/{id}/stop 停止
3. 再次查看沙箱状态
4. 多次重复 stop 调用（验证幂等性）
```

**验证点**：
- stop 后 sandbox 仍然存在，GET 返回 200
- 状态变为 `idle`（无运行 session）
- cargo 数据卷保留，下次使用可恢复
- stop 是幂等的，重复调用不报错

**实际日志片段**：
```
sandbox.stop sandbox_id=sandbox-c9eb112695c3
session.stop session_id=sess-1f62fd733cf0
docker.stop container_id=4c5c4c4bdbad...
# 注意：没有 cargo.delete，数据保留
```

### 2.3 E2E-03: Delete（彻底销毁）

**模拟场景**：用户完成任务，彻底删除沙箱和数据

**用户操作流程**：
```
1. 创建沙箱（自动创建 managed cargo）
2. 执行代码，产生一些数据
3. 调用 DELETE /v1/sandboxes/{id}
4. 尝试 GET 查看（应返回 404）
5. 检查 Docker volume 是否被删除
```

**验证点**：
- delete 返回 204
- 后续 GET 返回 404（软删除）
- Docker 容器被销毁
- managed cargo 对应的 Docker volume 被删除

**实际日志片段**：
```
sandbox.delete sandbox_id=sandbox-0eae1659a4b2
session.destroy session_id=sess-1831b311500b
docker.destroy container_id=5dc4f2cbdd80...
cargo.delete cargo_id=ws-ff0be9ea2284
docker.delete_volume name=bay-cargo-ws-ff0be9ea2284
# volume 真实删除，数据清理
```

### 2.4 E2E-04: 并发 ensure_running

**模拟场景**：LLM Agent 并发发起多个执行请求

**用户操作流程**：
```
1. 创建沙箱（尚无 session）
2. 同时发起 5 个 python/exec 请求
3. 观察响应和最终状态
```

**验证点**：
- 部分请求成功执行（200）
- 部分请求收到 503（session_not_ready），客户端应重试
- 不会造成严重错误或数据不一致

## 3. 发现的问题及修复

### 3.1 已修复：Sandbox.cargo_id 外键约束冲突

**问题描述**：
删除 sandbox 时，managed cargo 需要级联删除。但由于 `Sandbox.cargo_id` 字段定义为 NOT NULL，当 cargo 被删除时，SQLAlchemy 尝试将 sandbox 的 cargo_id 设为 NULL，触发约束违反。

**错误信息**：
```
sqlite3.IntegrityError: NOT NULL constraint failed: sandboxes.cargo_id
```

**修复方案**：
将 `Sandbox.cargo_id` 改为 `Optional[str]`，允许软删除后的 sandbox 的 cargo_id 为 NULL。

**代码变更**：[`pkgs/bay/app/models/sandbox.py`](../pkgs/bay/app/models/sandbox.py:44)
```python
# 修改前
cargo_id: str = Field(foreign_key="cargos.id", index=True)

# 修改后
cargo_id: Optional[str] = Field(
    default=None, foreign_key="cargos.id", index=True
)
```

**影响分析**：
- 活跃 sandbox（deleted_at IS NULL）在创建时保证 cargo_id 非空
- 已删除 sandbox 的 API 路由会先被 404 拦截，不会访问 ensure_running 等代码
- 仅影响软删除记录（用于审计），不影响正常业务逻辑

### 3.2 已修复：并发 ensure_running 创建多个 session (**2026-01-29**)

**问题描述**：
当多个请求同时到达时，`ensure_running` 为同一个 sandbox 创建多个 session 和容器。

**日志证据（修复前）**：
```
session.create session_id=sess-fb52712af35b  # 并发创建
session.create session_id=sess-9caf273ca779  # 并发创建
session.create session_id=sess-bedf68d1c217  # 并发创建
```

**修复方案**：
在 `SandboxManager.ensure_running()` 中实现双层并发控制：

1. **内存锁** - 使用 `asyncio.Lock` 序列化同一 sandbox 的并发请求
2. **事务刷新** - 在锁内调用 `await self._db.rollback()` 强制开始新事务，读取最新数据
3. **双重检查** - 重新查询 sandbox 并检查 `current_session_id`，避免重复创建

**代码变更**：[`pkgs/bay/app/managers/sandbox/sandbox.py`](../pkgs/bay/app/managers/sandbox/sandbox.py:206)
```python
# 模块级锁映射
_sandbox_locks: dict[str, asyncio.Lock] = {}

async def ensure_running(self, sandbox: Sandbox) -> Session:
    sandbox_id = sandbox.id
    cargo_id = sandbox.cargo_id
    
    sandbox_lock = await _get_sandbox_lock(sandbox_id)
    async with sandbox_lock:
        # 刷新事务以读取最新数据
        await self._db.rollback()
        
        # 重新查询（FOR UPDATE 用于 PostgreSQL）
        result = await self._db.execute(
            select(Sandbox).where(Sandbox.id == sandbox_id).with_for_update()
        )
        locked_sandbox = result.scalars().first()
        
        # 重新获取 cargo
        cargo = await self._cargo_mgr.get_by_id(cargo_id)
        
        # 双重检查：是否已有 session
        if locked_sandbox.current_session_id:
            session = await self._session_mgr.get(locked_sandbox.current_session_id)
        else:
            session = await self._session_mgr.create(...)
            locked_sandbox.current_session_id = session.id
            await self._db.commit()
        
        return await self._session_mgr.ensure_running(session, ...)
```

**日志证据（修复后）**：
```
session.create session_id=sess-abd80c933952  # 只创建 1 个
session.ensure_running observed_state=running session_id=sess-abd80c933952
session.ensure_running observed_state=running session_id=sess-abd80c933952
session.ensure_running observed_state=running session_id=sess-abd80c933952
session.ensure_running observed_state=running session_id=sess-abd80c933952
# 5 个并发请求全部成功复用同一个 session
```

## 4. 测试文件结构

```
pkgs/bay/tests/
├── conftest.py                    # pytest fixtures
├── fakes.py                       # FakeDriver 用于单元测试
├── unit/
│   ├── __init__.py
│   ├── test_sandbox_manager.py    # Unit-01/02/03
│   ├── test_docker_driver.py      # Unit-04
│   └── test_ship_client.py        # Unit-05
├── integration/
│   ├── __init__.py
│   └── test_e2e_api.py            # E2E-01/02/03/04
└── scripts/
    ├── README.md
    └── docker-host/               # docker host-port 模式
        ├── config.yaml            # 专用测试配置
        └── run.sh                 # 运行脚本
```

## 5. 运行测试

### 5.1 单元测试

```bash
cd pkgs/bay
uv run pytest tests/unit -v
```

### 5.2 E2E 测试

**前置条件**：
- Docker 可用
- `ship:latest` 镜像已构建（`cd pkgs/ship && make build`）

```bash
cd pkgs/bay
./tests/scripts/docker-host/run.sh       # 运行全部
./tests/scripts/docker-host/run.sh -v    # verbose 模式
./tests/scripts/docker-host/run.sh -k "test_create"  # 运行特定测试
```

## 6. 后续工作

根据 [tests.md](tests.md) 的 TODO 部分：

- [x] 加入 `GET /meta` 握手校验后，补充 capabilities 校验测试 (ShipAdapter.get_meta 已实现)
- [x] 加入 IdempotencyKey 后，补充 `POST /v1/sandboxes` 幂等测试 (**2026-01-29 已完成**)
- [ ] 修复并发 ensure_running 竞态问题后，更新并发测试断言

### 6.1 Idempotency 测试更新 (2026-01-29)

新增 `TestE2E06Idempotency` 测试类，包含 4 个测试：
- `test_idempotent_create_returns_same_response` - 相同 key 返回相同响应
- `test_idempotent_create_conflict_on_different_body` - 不同 body 返回 409
- `test_create_without_idempotency_key` - 无 key 正常创建多个
- `test_invalid_idempotency_key_format` - 无效 key 格式返回 409
