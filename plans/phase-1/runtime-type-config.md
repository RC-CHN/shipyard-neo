# Runtime Type 配置化设计

**状态**: ✅ 已完成
**优先级**: 低（向后兼容的增强）
**相关文件**: `config.py`, `session.py`, `config.yaml.example`

## 背景

当前 `SessionManager.create()` 中 `runtime_type` 被硬编码为 `"ship"`：

```python
# pkgs/bay/app/managers/session/session.py:72
session = Session(
    id=session_id,
    sandbox_id=sandbox_id,
    runtime_type="ship",  # ← 硬编码
    profile_id=profile.id,
    ...
)
```

这限制了 Bay 只能使用 Ship 运行时。为了支持未来的不同运行时类型（如 browser、gpu 等），需要将 `runtime_type` 配置化。

## 目标

- 将 `runtime_type` 从硬编码改为从 ProfileConfig 读取
- 保持向后兼容：现有配置无需修改即可继续工作
- 为未来扩展多运行时类型做好准备

## 改动方案

### 1. 修改 ProfileConfig（config.py）

```python
# pkgs/bay/app/config.py

class ProfileConfig(BaseModel):
    """Runtime profile configuration."""

    id: str
    image: str = "ship:latest"
    runtime_type: str = "ship"  # ← 新增字段，默认 "ship" 保持向后兼容
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    capabilities: list[str] = Field(default_factory=lambda: ["filesystem", "shell", "python"])
    idle_timeout: int = 1800
    runtime_port: int | None = 8123
    env: dict[str, str] = Field(default_factory=dict)
```

### 2. 修改 SessionManager.create()（session.py）

```python
# pkgs/bay/app/managers/session/session.py

async def create(
    self,
    sandbox_id: str,
    cargo: Cargo,
    profile: ProfileConfig,
) -> Session:
    session_id = f"sess-{uuid.uuid4().hex[:12]}"

    session = Session(
        id=session_id,
        sandbox_id=sandbox_id,
        runtime_type=profile.runtime_type,  # ← 改为从 profile 读取
        profile_id=profile.id,
        desired_state=SessionStatus.PENDING,
        observed_state=SessionStatus.PENDING,
        created_at=datetime.utcnow(),
        last_active_at=datetime.utcnow(),
    )
    ...
```

### 3. 更新 config.yaml.example

```yaml
profiles:
  - id: python-default
    image: "ship:latest"
    runtime_type: ship  # ← 新增，可选，默认 ship
    runtime_port: 8123
    resources:
      cpus: 1.0
      memory: "1g"
    capabilities:
      - filesystem
      - shell
      - python
    idle_timeout: 1800
    env: {}

  # 未来支持其他运行时的示例
  # - id: browser-default
  #   image: "bay-browser:latest"
  #   runtime_type: browser
  #   runtime_port: 9222
  #   capabilities:
  #     - browser
```

## 兼容性分析

| 组件 | 影响 | 说明 |
|------|------|------|
| CapabilityRouter._get_adapter() | ✅ 无需改动 | 已根据 session.runtime_type 选择 adapter |
| Session.runtime_type | ✅ 无需改动 | 字段已存在，只是值来源改变 |
| 现有配置文件 | ✅ 向后兼容 | 不指定 runtime_type 时默认 "ship" |
| 现有数据库 | ✅ 兼容 | Session 表中 runtime_type 字段不变 |

## 数据流图

```
config.yaml
    │
    ▼
┌──────────────────────────────────────┐
│ ProfileConfig                        │
│   id: python-default                 │
│   image: ship:latest                 │
│   runtime_type: ship  ←── 新增字段   │
│   runtime_port: 8123                 │
└──────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────┐
│ SessionManager.create                │
│   runtime_type=profile.runtime_type  │ ← 改为从 profile 读取
└──────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────┐
│ Session 模型                         │
│   runtime_type: str                  │ ← 存入数据库
└──────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────┐
│ CapabilityRouter._get_adapter        │
│   if runtime_type == ship:           │ ← 已有逻辑，无需改动
│       return ShipAdapter             │
│   elif runtime_type == browser:      │
│       return BrowserAdapter          │ ← 未来扩展点
└──────────────────────────────────────┘
```

## 未来扩展

支持新运行时只需：

1. 在配置中定义 profile：
   ```yaml
   - id: browser-default
     image: bay-browser:latest
     runtime_type: browser
     runtime_port: 9222
   ```

2. 创建对应的 Adapter：
   ```python
   class BrowserAdapter(BaseAdapter):
       async def health(self) -> bool: ...
       # 实现该运行时的 capability 方法
   ```

3. 在 CapabilityRouter 注册：
   ```python
   if session.runtime_type == "browser":
       self._adapters[endpoint] = BrowserAdapter(endpoint)
   ```

## 实施清单

- [x] 修改 `pkgs/bay/app/config.py` - ProfileConfig 添加 runtime_type 字段
- [x] 修改 `pkgs/bay/app/managers/session/session.py` - 使用 profile.runtime_type
- [x] 更新 `pkgs/bay/config.yaml.example` - 添加 runtime_type 示例
- [x] 更新 `pkgs/bay/config.yaml` - 添加 runtime_type
- [x] 更新 `pkgs/bay/tests/scripts/dev_server/config.yaml`
- [x] 更新 `pkgs/bay/tests/scripts/docker-host/config.yaml`
- [x] 更新 `pkgs/bay/tests/scripts/docker-network/config.yaml`
