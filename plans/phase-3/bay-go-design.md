# Bay Go 重写设计方案

> 用 Go 重写 Bay 编排层，追求最小内存占用与最快启动速度。

## 1. 重写目标

### 1.1 性能目标

| 指标 | Python 版本 | Go 版本目标 |
|:---|:---|:---|
| 内存占用 | ~150MB | ~10-30MB |
| 启动时间 | ~2s | ~20-80ms |
| 部署形态 | Python + 依赖 | 单一二进制 |

### 1.2 功能目标

- **100% API 兼容**：REST API 契约与 Python 版本完全一致
- **复用现有测试**：E2E 测试可直接验证 Go 版本
- **保持架构分层**：Driver / Manager / API 三层结构不变

### 1.3 非目标

- 不重写 Ship（核心依赖 IPython，保持 Python）
- 不重写 SDK（目标用户是 Python 开发者）
- 不引入新功能（先做到功能对等）

## 2. 技术选型

### 2.1 技术栈总览

| 层级 | 选型 | 理由 |
|:---|:---|:---|
| HTTP | Go 标准库 `net/http` | 零依赖，性能优秀 |
| 路由 | 最小化自研 mux | 避免框架依赖 |
| 数据库 | sqlc + database/sql | 类型安全 + 零运行时开销 |
| 迁移 | golang-migrate | 成熟稳定 |
| Docker | Docker Go SDK | 官方支持，功能完整 |
| 日志 | slog (标准库) | Go 1.21+ 内置 |
| 配置 | flag + 环境变量 | 最小依赖 |

### 2.2 数据层：sqlc

选择 sqlc 而非 ORM 的理由：

```
┌─────────────────────────────────────────────────────────────┐
│                      sqlc 工作流                             │
├─────────────────────────────────────────────────────────────┤
│  1. 编写 SQL schema (migrations/*.sql)                      │
│  2. 编写 SQL queries (queries/*.sql)                        │
│  3. sqlc generate → 生成类型安全的 Go 代码                   │
│  4. 编译时检查 SQL 语法与类型                                │
└─────────────────────────────────────────────────────────────┘
```

**sqlc vs ORM 对比**：

| 特性 | sqlc | GORM/Ent |
|:---|:---|:---|
| 运行时开销 | 无（编译时生成） | 有（反射） |
| 类型安全 | ✅ 编译时 | ✅ 运行时 |
| SQL 可控性 | 完全可控 | 抽象层 |
| 学习成本 | 低（写 SQL） | 中（学 ORM DSL） |
| 复杂查询 | 原生支持 | 需要 Raw SQL |

### 2.3 Docker Driver：Go SDK

选择 Docker Go SDK 而非 CLI 子进程：

| 方案 | 优点 | 缺点 |
|:---|:---|:---|
| **Docker Go SDK** ✅ | 类型安全、原生支持、错误处理完善 | 二进制增加 ~10MB |
| CLI 子进程 | 零依赖 | 解析输出复杂、错误处理弱 |

**优化策略**：

```go
// 1. 客户端单例复用
type DockerDriver struct {
    client *client.Client  // 线程安全，全局复用
    mu     sync.Mutex      // 按需保护
}

// 2. 减少 API 往返
// status() 使用 ContainerList 替代 ContainerInspect
containers, _ := cli.ContainerList(ctx, container.ListOptions{
    Filters: filters.NewArgs(filters.Arg("id", containerID)),
})

// 3. 并行 GC 操作
func (d *DockerDriver) BatchStop(ctx context.Context, ids []string) {
    var wg sync.WaitGroup
    for _, id := range ids {
        wg.Add(1)
        go func(cid string) {
            defer wg.Done()
            d.client.ContainerStop(ctx, cid, container.StopOptions{})
        }(id)
    }
    wg.Wait()
}
```

## 3. 项目结构

```
pkgs/bay-go/
├── cmd/
│   └── bay/
│       └── main.go              # 入口
├── internal/
│   ├── config/
│   │   └── config.go            # 配置管理
│   ├── db/
│   │   ├── db.go                # 数据库连接
│   │   ├── migrations/          # SQL 迁移文件
│   │   │   ├── 001_init.up.sql
│   │   │   └── 001_init.down.sql
│   │   └── queries/             # sqlc 查询定义
│   │       ├── sandbox.sql
│   │       ├── session.sql
│   │       ├── workspace.sql
│   │       └── idempotency.sql
│   ├── repo/                    # sqlc 生成的代码
│   │   ├── db.go
│   │   ├── models.go
│   │   ├── sandbox.sql.go
│   │   ├── session.sql.go
│   │   └── ...
│   ├── driver/
│   │   ├── driver.go            # Driver 接口
│   │   └── docker/
│   │       └── docker.go        # Docker 实现
│   ├── manager/
│   │   ├── sandbox.go           # SandboxManager
│   │   ├── session.go           # SessionManager
│   │   └── workspace.go         # WorkspaceManager
│   ├── router/
│   │   └── capability.go        # CapabilityRouter
│   ├── client/
│   │   └── ship.go              # ShipClient
│   ├── api/
│   │   ├── router.go            # HTTP 路由
│   │   ├── middleware.go        # 中间件（auth, logging）
│   │   ├── sandbox.go           # /v1/sandboxes handlers
│   │   ├── capability.go        # 能力代理 handlers
│   │   └── health.go            # /health
│   └── auth/
│       └── auth.go              # 认证逻辑
├── sqlc.yaml                    # sqlc 配置
├── go.mod
├── go.sum
├── Makefile
└── Dockerfile
```

## 4. 核心模块设计

### 4.1 数据模型（sqlc schema）

```sql
-- migrations/001_init.up.sql

CREATE TABLE sandboxes (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    ttl_seconds INTEGER,
    expires_at TIMESTAMP,
    idle_expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    container_id TEXT,
    endpoint TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (sandbox_id) REFERENCES sandboxes(id)
);

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    driver_ref TEXT NOT NULL,
    managed BOOLEAN NOT NULL DEFAULT FALSE,
    managed_by_sandbox_id TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE idempotency_keys (
    key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_body TEXT,
    response_status INTEGER,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX idx_sandboxes_owner ON sandboxes(owner);
CREATE INDEX idx_sandboxes_expires_at ON sandboxes(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_sessions_sandbox_id ON sessions(sandbox_id);
CREATE INDEX idx_workspaces_owner ON workspaces(owner);
CREATE INDEX idx_idempotency_expires ON idempotency_keys(expires_at);
```

### 4.2 sqlc 查询定义

```sql
-- queries/sandbox.sql

-- name: GetSandbox :one
SELECT * FROM sandboxes WHERE id = ? AND owner = ?;

-- name: ListSandboxes :many
SELECT * FROM sandboxes WHERE owner = ? ORDER BY created_at DESC;

-- name: CreateSandbox :one
INSERT INTO sandboxes (id, owner, profile_id, workspace_id, status, ttl_seconds, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
RETURNING *;

-- name: UpdateSandboxStatus :exec
UPDATE sandboxes SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;

-- name: UpdateIdleExpiresAt :exec
UPDATE sandboxes SET idle_expires_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;

-- name: DeleteSandbox :exec
DELETE FROM sandboxes WHERE id = ?;

-- name: GetExpiredSandboxes :many
SELECT * FROM sandboxes
WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP AND status != 'expired';

-- name: GetIdleSandboxes :many
SELECT * FROM sandboxes
WHERE idle_expires_at IS NOT NULL AND idle_expires_at < CURRENT_TIMESTAMP AND status = 'running';
```

### 4.3 Driver 接口

```go
// internal/driver/driver.go

package driver

import (
    "context"
)

type ContainerStatus string

const (
    StatusCreated  ContainerStatus = "created"
    StatusRunning  ContainerStatus = "running"
    StatusExited   ContainerStatus = "exited"
    StatusNotFound ContainerStatus = "not_found"
)

type ContainerInfo struct {
    ContainerID string
    Status      ContainerStatus
    Endpoint    string
    ExitCode    *int
}

type CreateParams struct {
    SessionID   string
    SandboxID   string
    WorkspaceID string
    ProfileID   string
    Image       string
    RuntimePort int
    Env         map[string]string
    Memory      int64
    CPUs        float64
    Labels      map[string]string
}

type Driver interface {
    Create(ctx context.Context, params CreateParams) (containerID string, err error)
    Start(ctx context.Context, containerID string, runtimePort int) (endpoint string, err error)
    Stop(ctx context.Context, containerID string) error
    Destroy(ctx context.Context, containerID string) error
    Status(ctx context.Context, containerID string, runtimePort int) (ContainerInfo, error)

    // Volume 操作
    CreateVolume(ctx context.Context, name string, labels map[string]string) error
    DeleteVolume(ctx context.Context, name string) error
    VolumeExists(ctx context.Context, name string) (bool, error)
}
```

### 4.4 Manager 层

```go
// internal/manager/session.go

package manager

import (
    "context"
    "sync"
)

type SessionManager struct {
    repo   *repo.Queries
    driver driver.Driver
    locks  sync.Map  // sandbox_id -> *sync.Mutex
}

func (m *SessionManager) EnsureRunning(ctx context.Context, sandboxID string) (*repo.Session, error) {
    // 1. 获取 sandbox 级别锁
    lock := m.getLock(sandboxID)
    lock.Lock()
    defer lock.Unlock()

    // 2. 双重检查：是否已有 running session
    session, err := m.repo.GetActiveSession(ctx, sandboxID)
    if err == nil && session.Status == "running" {
        return session, nil
    }

    // 3. 创建新 session
    // ...
}

func (m *SessionManager) getLock(sandboxID string) *sync.Mutex {
    lock, _ := m.locks.LoadOrStore(sandboxID, &sync.Mutex{})
    return lock.(*sync.Mutex)
}
```

### 4.5 HTTP 路由

```go
// internal/api/router.go

package api

import (
    "net/http"
)

type Router struct {
    sandboxManager   *manager.SandboxManager
    sessionManager   *manager.SessionManager
    workspaceManager *manager.WorkspaceManager
    capabilityRouter *router.CapabilityRouter
    auth             *auth.Authenticator
}

func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
    // 简单路由匹配
    path := req.URL.Path
    method := req.Method

    switch {
    case method == "GET" && path == "/health":
        r.handleHealth(w, req)
    case method == "POST" && path == "/v1/sandboxes":
        r.handleCreateSandbox(w, req)
    case method == "GET" && path == "/v1/sandboxes":
        r.handleListSandboxes(w, req)
    case method == "GET" && matchPath(path, "/v1/sandboxes/*/"):
        r.handleGetSandbox(w, req)
    // ... 其他路由
    default:
        http.NotFound(w, req)
    }
}
```

## 5. 并发与一致性

### 5.1 锁策略

```
┌─────────────────────────────────────────────────────────────┐
│                    并发控制策略                              │
├─────────────────────────────────────────────────────────────┤
│  Session 操作：按 sandbox_id 粒度加锁 (sync.Map + Mutex)     │
│  DB 事务：使用数据库事务保证原子性                           │
│  幂等性：依赖 DB UNIQUE 约束                                │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 事务示例

```go
func (m *SandboxManager) Create(ctx context.Context, params CreateParams) (*Sandbox, error) {
    tx, err := m.db.BeginTx(ctx, nil)
    if err != nil {
        return nil, err
    }
    defer tx.Rollback()

    qtx := m.repo.WithTx(tx)

    // 1. 创建 workspace
    workspace, err := qtx.CreateWorkspace(ctx, ...)
    if err != nil {
        return nil, err
    }

    // 2. 创建 sandbox
    sandbox, err := qtx.CreateSandbox(ctx, ...)
    if err != nil {
        return nil, err
    }

    // 3. 创建 session
    session, err := qtx.CreateSession(ctx, ...)
    if err != nil {
        return nil, err
    }

    // 4. 提交事务
    if err := tx.Commit(); err != nil {
        return nil, err
    }

    // 5. 启动容器（事务外，失败不回滚 DB）
    go m.startSession(context.Background(), session)

    return sandbox, nil
}
```

## 6. 测试策略

### 6.1 测试矩阵

| 测试类型 | 数据库 | 范围 |
|:---|:---|:---|
| 单元测试 | SQLite in-memory | 每个 repository 的 CRUD |
| 事务测试 | SQLite in-memory | 原子性、并发 |
| 集成测试 | PostgreSQL 容器 | 迁移 + 查询语义 |
| E2E 测试 | 真实环境 | 复用 Python 版本的 E2E |
| 安全测试 | SQLite | SQL 注入回归 |

### 6.2 SQL 注入测试

```go
func TestSQLInjection(t *testing.T) {
    maliciousInputs := []string{
        "'; DROP TABLE sandboxes; --",
        "' OR 1=1 --",
        "1; DELETE FROM sessions WHERE 1=1",
    }

    for _, input := range maliciousInputs {
        t.Run(input, func(t *testing.T) {
            // sqlc 生成的代码使用参数化查询
            // 这些输入应该被当作字面值处理
            _, err := repo.GetSandbox(ctx, input, "owner")
            // 应该返回 not found，而不是 SQL 错误或执行注入
            assert.ErrorIs(t, err, sql.ErrNoRows)
        })
    }
}
```

### 6.3 性能基准

```go
func BenchmarkCreateSandbox(b *testing.B) {
    // 对比 Python 版本的 QPS
}

func BenchmarkStartupTime(b *testing.B) {
    // 测量冷启动时间
}
```

## 7. 迁移策略

### 7.1 数据库迁移

Python 版本使用 SQLModel/Alembic，Go 版本使用 golang-migrate：

```bash
# 生成迁移文件（从 Python schema 导出）
migrate create -ext sql -dir migrations -seq init

# 应用迁移
migrate -path migrations -database "sqlite3://bay.db" up

# 回滚
migrate -path migrations -database "sqlite3://bay.db" down 1
```

### 7.2 渐进式切换

```
阶段 1：开发验证
  └── Go 版本通过所有 E2E 测试

阶段 2：影子运行
  └── Go 版本接收流量镜像，对比响应

阶段 3：金丝雀发布
  └── 10% 流量切换到 Go 版本

阶段 4：全量切换
  └── 100% 流量，Python 版本下线
```

## 8. 构建与部署

### 8.1 Makefile

```makefile
.PHONY: generate build test lint

# sqlc 代码生成
generate:
	sqlc generate

# 构建
build: generate
	CGO_ENABLED=0 go build -ldflags="-s -w" -o bin/bay ./cmd/bay

# 测试
test:
	go test -race -v ./...

# 静态检查
lint:
	golangci-lint run
	gosec ./...

# Docker 镜像
docker:
	docker build -t bay-go:latest .
```

### 8.2 Dockerfile

```dockerfile
# 构建阶段
FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o bay ./cmd/bay

# 运行阶段
FROM alpine:3.19
RUN apk --no-cache add ca-certificates
COPY --from=builder /app/bay /usr/local/bin/bay
EXPOSE 8000
ENTRYPOINT ["bay"]
```

### 8.3 二进制大小优化

```bash
# 基础构建
go build -o bay ./cmd/bay
# ~15-20MB

# 去除调试信息
go build -ldflags="-s -w" -o bay ./cmd/bay
# ~10-15MB

# UPX 压缩（可选，增加启动时间）
upx --best bay
# ~4-6MB
```

## 9. SDK 生成策略

Bay Go 重写后，SDK 通过 OpenAPI spec 自动生成：

```bash
# 从 Go 代码生成 OpenAPI spec（使用注解）
swag init -g cmd/bay/main.go -o api/docs

# 或复用现有 OpenAPI spec
# 从 spec 生成多语言 SDK
openapi-generator generate -i api/openapi.yaml -g python -o sdk-python/
openapi-generator generate -i api/openapi.yaml -g go -o sdk-go/
openapi-generator generate -i api/openapi.yaml -g typescript-fetch -o sdk-ts/
```

## 10. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|:---|:---|:---|
| API 不兼容 | 破坏现有 SDK/客户端 | 复用现有 E2E 测试验证 |
| 数据库迁移失败 | 数据丢失 | 先备份，渐进式迁移 |
| 并发 bug | 数据不一致 | 完善的并发测试 + race detector |
| Docker SDK 版本兼容 | 部分 Docker 版本不支持 | 使用 API 版本协商 |

## 11. 里程碑

```
M1: 项目骨架 + 数据层
  ├── sqlc 配置与 schema
  ├── 迁移文件
  └── Repository 层单元测试

M2: Driver 层
  ├── Docker Driver 实现
  ├── 容器生命周期测试
  └── Volume 操作测试

M3: Manager 层
  ├── SandboxManager
  ├── SessionManager
  ├── WorkspaceManager
  └── 并发测试

M4: API 层
  ├── HTTP 路由
  ├── 认证中间件
  └── 错误处理

M5: E2E 验证
  ├── 复用现有 E2E 测试
  ├── 性能基准对比
  └── 文档更新
```

## 12. 参考资料

- [sqlc 文档](https://docs.sqlc.dev/)
- [Docker Go SDK](https://pkg.go.dev/github.com/docker/docker/client)
- [golang-migrate](https://github.com/golang-migrate/migrate)
- [现有 Bay 设计文档](../bay-design.md)
- [现有 Bay API 文档](../bay-api.md)
