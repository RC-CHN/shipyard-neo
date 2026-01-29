# Bay 鉴权设计（Phase 1）

> 更新日期：2026-01-29 16:00 (UTC+8)
>
> 相关设计：
> - [`phase-1.md`](phase-1.md) - Phase 1 总体进度
> - [`../bay-api.md`](../bay-api.md) - API 规范
> - [`../bay-design.md`](../bay-design.md:384) - 安全策略章节

---

## 1. 背景与现状

### 1.1 当前代码结构

Bay 的认证框架已经预留，但逻辑未实现：

| 组件 | 文件 | 状态 |
|------|------|------|
| 认证函数 | [`dependencies.py:59`](../../pkgs/bay/app/api/dependencies.py:59) | `get_current_owner()` 空实现 |
| 类型别名 | [`dependencies.py:85`](../../pkgs/bay/app/api/dependencies.py:85) | `OwnerDep` 已定义 |
| 安全配置 | [`config.py:121`](../../pkgs/bay/app/config.py:121) | `SecurityConfig` 含 JWT 配置（未使用） |
| Manager 隔离 | `managers/sandbox/sandbox.py` | 所有查询带 `owner == owner` |
| API 端点 | `api/v1/*.py` | 17 处使用 `OwnerDep` |

### 1.2 已移除 `get_current_owner()`，改为 `authenticate()`

```python
# pkgs/bay/app/api/dependencies.py:59
from app.errors import UnauthorizedError

def authenticate(request: Request) -> str:
    ...
```

要点：
- Bearer token 存在时：如果配置了 `api_key`，则必须匹配，否则 401
- Bearer token 不存在时：仅当 `allow_anonymous=true` 才允许访问
- `X-Owner` 仅在 `allow_anonymous=true` 时生效（开发测试）

### 1.3 `SecurityConfig` 已更新（删除 JWT）

```python
# pkgs/bay/app/config.py:121
class SecurityConfig(BaseModel):
    api_key: str | None = None
    allow_anonymous: bool = True
    blocked_hosts: list[str] = Field(...)
```

同时更新了：
- [`pkgs/bay/config.yaml`](../../pkgs/bay/config.yaml:26)
- [`pkgs/bay/config.yaml.example`](../../pkgs/bay/config.yaml.example:46)
- `tests/scripts/*/config.yaml`

### 1.4 `OwnerDep` → `AuthDep`

共 17 处引用（已更新为 `AuthDep`）：

| 文件 | 端点数 |
|------|--------|
| [`sandboxes.py`](../../pkgs/bay/app/api/v1/sandboxes.py:14) | 6 |
| [`capabilities.py`](../../pkgs/bay/app/api/v1/capabilities.py:16) | 8 |
| [`dependencies.py`](../../pkgs/bay/app/api/dependencies.py:1) | 1 (定义) |

---

## 2. 设计决策

### 2.1 认证方式：API Key（非 JWT）

| 因素 | 分析 |
|------|------|
| 多租户需求 | ❌ 暂无，单租户自托管 |
| Token 轮换 | ❌ 暂无需求 |
| 用户身份 | ❌ owner 固定为 `"default"` |
| 复杂度 | JWT 需要签发/刷新逻辑 |

**结论**：采用固定 **API Key** 方案，简单够用。

### 2.2 保留 owner 字段

虽然 owner 固定，但保留的原因：

1. **深度耦合**：Model、Manager、API 共 75+ 处引用
2. **改动大**：移除需要改 Model schema、数据库迁移
3. **预留扩展**：未来加多租户只需改认证函数

**结论**：保留 owner 字段，认证通过后返回 `"default"`。

### 2.3 函数/类型重命名

当前命名 `get_current_owner()` / `OwnerDep` 在单租户场景下语义不准确。

| 原名 | 新名 | 说明 |
|------|------|------|
| `get_current_owner()` | `authenticate()` | 函数职责是认证 |
| `OwnerDep` | `AuthDep` | 更通用的类型名 |

### 2.4 X-Owner header 处理

| 方案 | 说明 |
|------|------|
| ❌ 移除 | 失去测试灵活性 |
| ✅ 保留（仅开发模式） | `allow_anonymous=true` 时可用 |

---

## 3. 目标方案

### 3.1 配置变更

**修改前**（[`config.py:121`](../../pkgs/bay/app/config.py:121)）：

```python
class SecurityConfig(BaseModel):
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    blocked_hosts: list[str] = Field(...)
```

**修改后**：

```python
class SecurityConfig(BaseModel):
    """Security configuration."""

    # API Key 认证
    # None = 禁用认证检查（仅检查 allow_anonymous）
    api_key: str | None = None
    
    # 是否允许无认证访问
    # 开发模式: True（默认）
    # 生产环境: False
    allow_anonymous: bool = True
    
    # 网络黑名单（Phase 2 使用）
    blocked_hosts: list[str] = Field(default_factory=lambda: [
        "169.254.0.0/16",  # Cloud metadata
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ])
```

### 3.2 认证函数

**修改前**（[`dependencies.py:59`](../../pkgs/bay/app/api/dependencies.py:59)）：

```python
def get_current_owner(request: Request) -> str:
    ...
    return "default"

OwnerDep = Annotated[str, Depends(get_current_owner)]
```

**修改后**：

```python
from app.errors import UnauthorizedError

def authenticate(request: Request) -> str:
    """Authenticate request and return owner.
    
    Single-tenant mode: Always returns "default" as owner.
    
    Authentication flow:
    1. If Bearer token provided → validate API key
    2. If no token and allow_anonymous → allow
    3. Otherwise → 401 Unauthorized
    
    Returns:
        Owner identifier (currently fixed to "default")
    
    Raises:
        UnauthorizedError: If authentication fails
    """
    settings = get_settings()
    security = settings.security
    auth_header = request.headers.get("Authorization")
    
    # 1. Bearer token provided
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        
        # Validate API Key (if configured)
        if security.api_key:
            if token == security.api_key:
                return "default"  # Single-tenant, fixed owner
            raise UnauthorizedError("Invalid API key")
        
        # No API key configured, accept any token in anonymous mode
        if security.allow_anonymous:
            return "default"
        raise UnauthorizedError("Authentication required")
    
    # 2. No token - check anonymous mode
    if security.allow_anonymous:
        # Development mode: allow X-Owner header for testing
        owner = request.headers.get("X-Owner")
        if owner:
            return owner
        return "default"
    
    # 3. Production mode, authentication required
    raise UnauthorizedError("Authentication required")


# Type alias for dependency injection
AuthDep = Annotated[str, Depends(authenticate)]
```

### 3.3 API 端点更新

更新所有引用 `OwnerDep` 的地方：

**`sandboxes.py`**：

```python
# 修改前
from app.api.dependencies import IdempotencyServiceDep, OwnerDep, SandboxManagerDep
...
owner: OwnerDep,

# 修改后
from app.api.dependencies import AuthDep, IdempotencyServiceDep, SandboxManagerDep
...
owner: AuthDep,  # 参数名保持 owner（语义清晰）
```

**`capabilities.py`**：

```python
# 修改前
from app.api.dependencies import OwnerDep, SandboxManagerDep

# 修改后  
from app.api.dependencies import AuthDep, SandboxManagerDep
```

### 3.4 配置文件示例

**`config.yaml.example`**：

```yaml
security:
  # API Key 认证
  # 设置后所有请求必须带 Authorization: Bearer <api_key>
  # 不设置或设为 null 则不验证 token 内容
  api_key: null  # 生产环境: "your-secret-api-key"
  
  # 是否允许无认证访问
  # 开发环境: true（默认）
  # 生产环境: false
  allow_anonymous: true
  
  # 网络黑名单（Phase 2 使用）
  blocked_hosts:
    - "169.254.0.0/16"
    - "10.0.0.0/8"
    - "172.16.0.0/12"
    - "192.168.0.0/16"
```

---

## 4. 行为矩阵

| `api_key` | `allow_anonymous` | 无 header | 正确 key | 错误 key |
|-----------|-------------------|-----------|----------|----------|
| `null` | `true` | ✅ 200 | ✅ 200 | ✅ 200 |
| `null` | `false` | ❌ 401 | ❌ 401 | ❌ 401 |
| `"secret"` | `true` | ✅ 200 | ✅ 200 | ❌ 401 |
| `"secret"` | `false` | ❌ 401 | ✅ 200 | ❌ 401 |

**推荐配置**：
- 开发：`api_key: null`, `allow_anonymous: true`
- 生产：`api_key: "your-key"`, `allow_anonymous: false`

---

## 5. 实现清单

### 5.1 Python 代码修改

| 文件 | 修改内容 | 行数估计 |
|------|---------|---------|
| [`app/config.py`](../../pkgs/bay/app/config.py:121) | 修改 `SecurityConfig`：删除 JWT 配置，添加 `api_key`、`allow_anonymous` | ~10 行 |
| [`app/api/dependencies.py`](../../pkgs/bay/app/api/dependencies.py:59) | 重命名函数 `authenticate()`、类型 `AuthDep`，实现认证逻辑 | ~40 行 |
| [`app/api/v1/sandboxes.py`](../../pkgs/bay/app/api/v1/sandboxes.py:14) | import + 6 处 `OwnerDep` → `AuthDep` | ~7 行 |
| [`app/api/v1/capabilities.py`](../../pkgs/bay/app/api/v1/capabilities.py:16) | import + 10 处 `OwnerDep` → `AuthDep` | ~11 行 |

### 5.2 配置文件修改

| 文件 | 修改内容 |
|------|---------|
| [`config.yaml`](../../pkgs/bay/config.yaml:27) | 删除 JWT 配置，添加 `api_key`、`allow_anonymous` |
| [`config.yaml.example`](../../pkgs/bay/config.yaml.example:46) | 同上 |
| [`tests/scripts/dev_server/config.yaml`](../../pkgs/bay/tests/scripts/dev_server/config.yaml:35) | 同上 |
| [`tests/scripts/docker-host/config.yaml`](../../pkgs/bay/tests/scripts/docker-host/config.yaml:35) | 同上 |
| [`tests/scripts/docker-network/config.yaml`](../../pkgs/bay/tests/scripts/docker-network/config.yaml:36) | 同上 |

### 5.3 新建文件

| 文件 | 内容 |
|------|------|
| `tests/unit/test_auth.py` | 认证单元测试 |

### 5.4 删除内容

| 文件 | 删除内容 |
|------|---------|
| `app/config.py` | `jwt_secret`、`jwt_algorithm`、`jwt_expire_minutes` |
| 各 `config.yaml` | 对应的 JWT 配置行 |

---

## 6. 测试计划

### 6.1 单元测试

```python
# tests/unit/test_auth.py
import pytest
from fastapi.testclient import TestClient

class TestAuthentication:
    """Test API Key authentication."""
    
    def test_anonymous_allowed(self, client_anonymous):
        """Anonymous access when allow_anonymous=true."""
        response = client_anonymous.get("/v1/sandboxes")
        assert response.status_code == 200
    
    def test_anonymous_denied(self, client_strict):
        """Anonymous denied when allow_anonymous=false."""
        response = client_strict.get("/v1/sandboxes")
        assert response.status_code == 401
    
    def test_valid_api_key(self, client_with_key):
        """Valid API key accepted."""
        response = client_with_key.get(
            "/v1/sandboxes",
            headers={"Authorization": "Bearer test-key"}
        )
        assert response.status_code == 200
    
    def test_invalid_api_key(self, client_with_key):
        """Invalid API key rejected."""
        response = client_with_key.get(
            "/v1/sandboxes",
            headers={"Authorization": "Bearer wrong-key"}
        )
        assert response.status_code == 401
    
    def test_x_owner_in_anonymous_mode(self, client_anonymous):
        """X-Owner header works in anonymous mode."""
        response = client_anonymous.post(
            "/v1/sandboxes",
            json={"profile": "python-default"},
            headers={"X-Owner": "test-user"}
        )
        assert response.status_code == 201
    
    def test_x_owner_ignored_with_api_key(self, client_with_key):
        """X-Owner header ignored when API key provided."""
        response = client_with_key.post(
            "/v1/sandboxes",
            json={"profile": "python-default"},
            headers={
                "Authorization": "Bearer test-key",
                "X-Owner": "other-user"  # 应该被忽略
            }
        )
        assert response.status_code == 201
        # owner 应该是 "default" 而不是 "other-user"
```

### 6.2 E2E 测试

已更新 E2E 测试默认携带 `Authorization: Bearer <E2E_API_KEY>`，并新增认证用例：
- [`TestE2E00Auth`](../../pkgs/bay/tests/integration/test_e2e_api.py:112)

测试脚本配置：
- docker-host / docker-network：`allow_anonymous=false` + `api_key=e2e-test-api-key`
- dev_server：`allow_anonymous=true` + `api_key=null`

---

## 7. 未来扩展

### 7.1 多租户（JWT）

```python
def authenticate(request: Request) -> str:
    ...
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        
        # 尝试 JWT 验证
        try:
            payload = jwt.decode(
                token,
                settings.security.jwt_secret,
                algorithms=[settings.security.jwt_algorithm],
            )
            return payload.get("sub") or payload.get("owner")
        except jwt.InvalidTokenError:
            pass
        
        # 回退到 API Key
        if security.api_key and token == security.api_key:
            return "default"
        
        raise UnauthorizedError("Invalid token")
    ...
```

### 7.2 RBAC（角色权限）

```python
@dataclass
class Identity:
    owner: str
    roles: list[str]  # ["admin", "user", "viewer"]

def authenticate(request: Request) -> Identity:
    ...
    return Identity(owner="alice", roles=["user"])
```

### 7.3 审计日志

```python
@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    
    await audit_log.record(
        request_id=request.state.request_id,
        owner=getattr(request.state, "owner", None),
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=(time.time() - start) * 1000,
    )
    return response
```

---

## 8. 待决策问题

所有关键问题已决策：

| # | 问题 | 决策 |
|---|------|------|
| 1 | 认证方式 | ✅ API Key |
| 2 | 函数重命名 | ✅ `authenticate()` |
| 3 | 类型重命名 | ✅ `AuthDep` |
| 4 | X-Owner 处理 | ✅ 保留（仅开发模式） |
| 5 | JWT 配置 | ✅ 删除 |

---

## 9. Checklist

### Phase 1 实现

- [x] 修改 `SecurityConfig`
- [x] 修改 `authenticate()` 函数
- [x] 重命名 `OwnerDep` → `AuthDep`
- [x] 更新 `sandboxes.py` (6 处)
- [x] 更新 `capabilities.py` (8 处)
- [x] 更新 `config.yaml.example`
- [x] 更新 `config.yaml`
- [x] 更新测试配置文件 (3 个)
- [x] 新建 [`tests/unit/test_auth.py`](../../pkgs/bay/tests/unit/test_auth.py:1)（18 tests）
- [x] 运行单元测试（91 passed）
- [x] 运行 E2E 测试（docker-host / docker-network）

### Phase 2 遗留

- [ ] 路径安全校验
- [ ] 网络黑名单执行
- [ ] 审计日志
- [ ] JWT 多租户支持（如需；Phase 2+）
