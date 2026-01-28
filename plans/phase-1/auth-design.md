# Bay 鉴权设计与实现状态

> 本文详细记录 Bay 的鉴权设计、当前实现进度、预留扩展点，以及后续实现计划。
>
> 相关设计：
> - [`plans/bay-design.md`](../bay-design.md:384) - 安全策略章节
> - [`plans/bay-api.md`](../bay-api.md:22) - 标准 Header 与错误模型
> - [`plans/phase-1/phase-1.md`](phase-1.md:115) - Phase 1 遗留项

## 0. 设计目标

1. **身份认证**：验证请求来源身份，提取 `owner` 标识
2. **资源隔离**：确保用户只能访问自己的资源（Sandbox/Workspace）
3. **安全边界**：防止容器逃逸、内网访问、路径穿越等攻击
4. **可扩展性**：支持多种认证方式（JWT/API Key/OAuth）

---

## 1. 已实现（Done）

### 1.1 Owner 依赖注入框架

**位置**：[`pkgs/bay/app/api/dependencies.py`](../../pkgs/bay/app/api/dependencies.py:46)

```python
def get_current_owner(request: Request) -> str:
    """Get current owner from request.
    
    TODO: Implement proper JWT authentication.
    For now, returns a default owner for development.
    """
    # Check for Authorization header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        # TODO: Validate JWT and extract owner
        pass

    # Check for X-Owner header (development only)
    owner = request.headers.get("X-Owner")
    if owner:
        return owner

    # Default owner for development
    return "default"


# Type alias for dependency injection
OwnerDep = Annotated[str, Depends(get_current_owner)]
```

**状态**：✅ 框架完成
- `OwnerDep` 已注入所有 API 端点
- 开发环境可通过 `X-Owner` header 传递 owner
- JWT 检测框架已预留（待填充验证逻辑）

---

### 1.2 Manager 层 Owner 隔离

**位置**：[`pkgs/bay/app/managers/sandbox/sandbox.py`](../../pkgs/bay/app/managers/sandbox/sandbox.py:116)

```python
async def get(self, sandbox_id: str, owner: str) -> Sandbox:
    """Get sandbox by ID - owner isolation enforced."""
    result = await self._db.execute(
        select(Sandbox).where(
            Sandbox.id == sandbox_id,
            Sandbox.owner == owner,        # ← 强制 owner 匹配
            Sandbox.deleted_at.is_(None),  # 软删除过滤
        )
    )
    sandbox = result.scalars().first()
    if sandbox is None:
        raise NotFoundError(f"Sandbox not found: {sandbox_id}")
    return sandbox


async def list(self, owner: str, ...) -> tuple[list[Sandbox], str | None]:
    """List sandboxes - only returns owner's resources."""
    query = select(Sandbox).where(
        Sandbox.owner == owner,            # ← 强制 owner 匹配
        Sandbox.deleted_at.is_(None),
    )
    ...
```

**状态**：✅ 完成
- `SandboxManager.get()` - owner 隔离
- `SandboxManager.list()` - owner 过滤
- `WorkspaceManager.get()` - owner 隔离
- `WorkspaceManager.list()` - owner 过滤

**隔离效果**：
- 用户 A 无法访问用户 B 的 sandbox（即使知道 sandbox_id）
- 返回 `NotFoundError (404)` 而非 `ForbiddenError (403)`，避免信息泄露

---

### 1.3 安全配置项

**位置**：[`pkgs/bay/app/config.py`](../../pkgs/bay/app/config.py:114)

```python
class SecurityConfig(BaseModel):
    """Security configuration."""

    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    blocked_hosts: list[str] = Field(
        default_factory=lambda: [
            "169.254.0.0/16",   # AWS/GCP 元数据服务
            "10.0.0.0/8",       # 私有网络
            "172.16.0.0/12",    # 私有网络
            "192.168.0.0/16",   # 私有网络
        ]
    )
```

**状态**：✅ 配置完成
- JWT 配置项已定义
- 网络黑名单已定义
- 支持环境变量覆盖：`BAY_SECURITY__JWT_SECRET`

---

### 1.4 Request ID 贯穿

**位置**：[`pkgs/bay/app/main.py`](../../pkgs/bay/app/main.py:44)

```python
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Add request ID to all requests."""
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response
```

**状态**：✅ 完成
- 客户端可传 `X-Request-Id`
- 服务端自动生成或回传
- 可用于审计日志关联

---

### 1.5 统一错误模型

**位置**：[`pkgs/bay/app/errors.py`](../../pkgs/bay/app/errors.py:1)

```python
class BayError(Exception):
    """Base error for Bay."""
    code: str
    message: str
    status_code: int

class NotFoundError(BayError):      # 404
class UnauthorizedError(BayError):  # 401
class ForbiddenError(BayError):     # 403
class ValidationError(BayError):    # 400
```

**状态**：✅ 完成
- 错误类已定义
- `BayError` handler 已注册（返回统一 JSON 格式）
- 符合 [`plans/bay-api.md`](../bay-api.md:80) 规范

---

### 1.6 幂等键模型

**位置**：[`pkgs/bay/app/models/idempotency.py`](../../pkgs/bay/app/models/idempotency.py:1)

```python
class IdempotencyKey(SQLModel, table=True):
    """Idempotency key for request deduplication."""
    __tablename__ = "idempotency_keys"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)
    method: str
    path: str
    request_hash: str
    response_snapshot: str | None
    status_code: int | None
    created_at: datetime
    expires_at: datetime
```

**状态**：✅ 模型完成（API 层未接入）

---

## 2. 预留扩展点（Extension Points）

### 2.1 认证方式扩展

**当前入口**：[`get_current_owner()`](../../pkgs/bay/app/api/dependencies.py:46)

支持的扩展：
1. **JWT Bearer Token** - 标准方案，已预留框架
2. **API Key** - 简单方案，适合服务间调用
3. **OAuth 2.0** - 第三方集成
4. **mTLS** - 高安全场景

**扩展方式**：
```python
def get_current_owner(request: Request) -> str:
    auth_header = request.headers.get("Authorization")
    
    # 1. JWT Bearer Token
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        return _validate_jwt(token)
    
    # 2. API Key
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return _validate_api_key(api_key)
    
    # 3. 开发模式
    if settings.is_dev:
        return request.headers.get("X-Owner", "default")
    
    raise UnauthorizedError("Authentication required")
```

---

### 2.2 权限模型扩展

当前：**单一 owner 维度**

未来可扩展：
- **RBAC**：角色（admin/user/viewer）
- **Team/Org**：多租户组织结构
- **Resource Policy**：细粒度资源权限

**扩展点**：
- `get_current_owner()` → `get_current_identity()` 返回结构化身份
- Manager 层增加权限检查 hook

---

### 2.3 审计日志扩展

**当前入口**：`request_id_middleware` + structlog

**扩展方式**：
```python
# 新增审计 middleware
@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    
    await audit_log.record(
        request_id=request.state.request_id,
        owner=request.state.owner,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=(time.time() - start) * 1000,
    )
    return response
```

---

## 3. 待实现（TODO）

### 3.1 P0 - Phase 1 必须完成

#### 3.1.1 JWT Token 验证

**位置**：[`pkgs/bay/app/api/dependencies.py`](../../pkgs/bay/app/api/dependencies.py:46)

**实现方案**：
```python
import jwt
from app.config import get_settings
from app.errors import UnauthorizedError

def get_current_owner(request: Request) -> str:
    settings = get_settings()
    auth_header = request.headers.get("Authorization")
    
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = jwt.decode(
                token,
                settings.security.jwt_secret,
                algorithms=[settings.security.jwt_algorithm],
            )
            owner = payload.get("sub") or payload.get("owner")
            if not owner:
                raise UnauthorizedError("Invalid token: missing owner")
            return owner
        except jwt.ExpiredSignatureError:
            raise UnauthorizedError("Token expired")
        except jwt.InvalidTokenError as e:
            raise UnauthorizedError(f"Invalid token: {e}")
    
    # Development fallback
    if request.headers.get("X-Owner"):
        # TODO: 生产环境应禁用
        return request.headers.get("X-Owner")
    
    raise UnauthorizedError("Authentication required")
```

**依赖**：`pyjwt` 已在 `pyproject.toml`

**估时**：0.5h

---

#### 3.1.2 Idempotency-Key 接入

**位置**：[`pkgs/bay/app/api/v1/sandboxes.py`](../../pkgs/bay/app/api/v1/sandboxes.py:72)

**实现方案**：
```python
from app.models.idempotency import IdempotencyKey

@router.post("", response_model=SandboxResponse, status_code=201)
async def create_sandbox(
    request: CreateSandboxRequest,
    sandbox_mgr: SandboxManagerDep,
    owner: OwnerDep,
    db: SessionDep,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> SandboxResponse:
    # 检查幂等键
    if idempotency_key:
        existing = await db.get(IdempotencyKey, idempotency_key)
        if existing:
            if existing.owner != owner:
                raise ConflictError("Idempotency key conflict")
            # 返回缓存的响应
            return SandboxResponse.parse_raw(existing.response_snapshot)
    
    # 创建 sandbox
    sandbox = await sandbox_mgr.create(...)
    response = _sandbox_to_response(sandbox)
    
    # 保存幂等键
    if idempotency_key:
        key_record = IdempotencyKey(
            id=idempotency_key,
            owner=owner,
            method="POST",
            path="/v1/sandboxes",
            response_snapshot=response.json(),
            status_code=201,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(hours=24),
        )
        db.add(key_record)
        await db.commit()
    
    return response
```

**估时**：1h

---

#### 3.1.3 路径安全校验

**位置**：[`pkgs/bay/app/api/v1/capabilities.py`](../../pkgs/bay/app/api/v1/capabilities.py:1) 或新建 `utils/path_validator.py`

**实现方案**：
```python
import os
from app.errors import ValidationError

def validate_path(path: str) -> str:
    """Validate and normalize path for sandbox filesystem operations.
    
    Rules:
    1. Must be relative path (no leading /)
    2. No directory traversal (..)
    3. No null bytes
    4. Normalize separators
    
    Returns:
        Normalized path
    
    Raises:
        ValidationError: If path is invalid
    """
    if not path:
        raise ValidationError("Path cannot be empty")
    
    # 禁止绝对路径
    if path.startswith("/"):
        raise ValidationError("Absolute paths not allowed")
    
    # 禁止 null 字节
    if "\x00" in path:
        raise ValidationError("Null bytes not allowed in path")
    
    # 规范化并检查穿越
    normalized = os.path.normpath(path)
    if normalized.startswith("..") or "/../" in normalized or normalized == "..":
        raise ValidationError("Directory traversal not allowed")
    
    return normalized
```

**应用位置**：
- `POST /v1/sandboxes/{id}/files/read`
- `POST /v1/sandboxes/{id}/files/write`
- `POST /v1/sandboxes/{id}/files/list`
- `POST /v1/sandboxes/{id}/files/delete`

**估时**：0.5h

---

### 3.2 P1 - Phase 1 建议完成

#### 3.2.1 网络黑名单执行

**位置**：[`pkgs/bay/app/clients/runtime/ship.py`](../../pkgs/bay/app/clients/runtime/ship.py:1) 或 Driver 层

**实现方案**：
```python
import ipaddress
from app.config import get_settings

def is_blocked_host(host: str) -> bool:
    """Check if host is in blocked list."""
    settings = get_settings()
    
    try:
        ip = ipaddress.ip_address(host)
        for blocked in settings.security.blocked_hosts:
            network = ipaddress.ip_network(blocked)
            if ip in network:
                return True
    except ValueError:
        # hostname, not IP - resolve and check
        pass
    
    return False
```

**注意**：这个检查应该在容器内执行（Ship 侧），Bay 侧主要用于 Driver 层防护。

**估时**：1h

---

#### 3.2.2 命令审计

**实现方案**：在 `CapabilityRouter` 记录所有执行请求

```python
class CapabilityRouter:
    async def execute(self, sandbox_id: str, capability: str, operation: str, payload: dict) -> dict:
        # 审计日志
        self._log.info(
            "capability.execute",
            sandbox_id=sandbox_id,
            capability=capability,
            operation=operation,
            # 不记录敏感 payload，只记录摘要
            payload_keys=list(payload.keys()),
        )
        
        # ... 执行逻辑
```

**估时**：0.5h

---

### 3.3 P2 - Phase 2 延后

- K8s NetworkPolicy 配置
- 容器内 seccomp/AppArmor profile
- 执行命令白名单/黑名单
- 文件类型限制
- 速率限制（per-owner）
- Token 刷新机制

---

## 4. 安全检查清单

### 4.1 认证层

| 检查项 | 状态 | 说明 |
|:--|:--|:--|
| JWT Token 验证 | ❌ | 框架有，逻辑空 |
| Token 过期检查 | ❌ | 待实现 |
| Token 刷新 | ❌ | Phase 2 |
| API Key 支持 | ❌ | 可选 |

### 4.2 授权层

| 检查项 | 状态 | 说明 |
|:--|:--|:--|
| Owner 隔离 | ✅ | Manager 层已实现 |
| 资源归属检查 | ✅ | get/list 均校验 |
| 跨用户访问防护 | ✅ | 返回 404 不泄露 |

### 4.3 输入验证

| 检查项 | 状态 | 说明 |
|:--|:--|:--|
| 路径穿越防护 | ❌ | 待实现 |
| 绝对路径拒绝 | ❌ | 待实现 |
| 文件大小限制 | ❌ | 待实现 |
| 命令长度限制 | ❌ | 待实现 |

### 4.4 网络隔离

| 检查项 | 状态 | 说明 |
|:--|:--|:--|
| 内网访问阻断 | ❌ | 配置有，执行无 |
| 元数据服务阻断 | ❌ | 配置有，执行无 |
| 容器网络隔离 | ✅ | Docker network |

### 4.5 审计

| 检查项 | 状态 | 说明 |
|:--|:--|:--|
| Request ID 贯穿 | ✅ | 已实现 |
| 操作日志 | ⚠️ | 有 structlog，格式待规范 |
| 敏感操作审计 | ❌ | 待实现 |

---

## 5. 实现优先级与时间估算

### Phase 1 必须完成（估时 3h）

1. **JWT Token 验证** - 0.5h
2. **Idempotency-Key 接入** - 1h
3. **路径安全校验** - 0.5h
4. **单元测试覆盖** - 1h

### Phase 1 建议完成（估时 2h）

1. **网络黑名单执行** - 1h
2. **命令审计规范化** - 0.5h
3. **集成测试** - 0.5h

---

## 6. 参考资料

- [OWASP API Security Top 10](https://owasp.org/www-project-api-security/)
- [JWT Best Practices](https://datatracker.ietf.org/doc/html/rfc8725)
- [Docker Security Best Practices](https://docs.docker.com/develop/security-best-practices/)
