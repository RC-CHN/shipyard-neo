# Gull - Browser Runtime for Shipyard Bay

> 🦅 Gull (海鸥) - 航行在 Bay 港湾中的浏览器运行时

Gull 是一个轻量级 REST 服务，作为 `agent-browser` CLI 的 HTTP 代理运行在 Docker 容器中。它通过 CLI Passthrough 模式将 agent-browser 的 50+ 命令暴露为单一 REST API，避免逐一封装。

## 架构

```
┌──────────────────────────────────────────┐
│           Gull Container                  │
│                                           │
│  ┌─────────────────────────────────────┐ │
│  │     FastAPI REST Wrapper             │ │
│  │                                      │ │
│  │  POST /exec  → 执行 agent-browser   │ │
│  │  GET  /health → 健康检查            │ │
│  │  GET  /meta   → 运行时元数据        │ │
│  └─────────────────────────────────────┘ │
│                    │                      │
│                    ▼                      │
│  ┌─────────────────────────────────────┐ │
│  │        agent-browser CLI             │ │
│  │  自动注入 --session 参数             │ │
│  └─────────────────────────────────────┘ │
│                    │                      │
│                    ▼                      │
│          /workspace (Cargo Volume)        │
└──────────────────────────────────────────┘
```

## API

### `POST /exec` - 执行浏览器命令

```bash
curl -X POST http://localhost:8080/exec \
  -H 'Content-Type: application/json' \
  -d '{"cmd": "open https://example.com"}'
```

Response:
```json
{
  "stdout": "Navigated to https://example.com",
  "stderr": "",
  "exit_code": 0
}
```

### `GET /health` - 健康检查

```json
{
  "status": "healthy",
  "browser_active": true
}
```

### `GET /meta` - 运行时元数据

```json
{
  "runtime": {
    "name": "gull",
    "version": "0.1.0",
    "api_version": "v1"
  },
  "workspace": {
    "mount_path": "/workspace"
  },
  "capabilities": {
    "browser": {"version": "1.0"},
    "screenshot": {"version": "1.0"}
  }
}
```

## 开发

```bash
cd pkgs/gull
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

## Docker

```bash
docker build -t gull:latest .
docker run -p 8080:8080 -v my-workspace:/workspace gull:latest
```
