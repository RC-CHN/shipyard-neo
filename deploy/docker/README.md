# Docker Compose 生产部署

Bay + Ship + Gull 的 Docker Compose 自包含生产部署方案。

## 架构

```
┌──────────────────────────────────────────────────────┐
│  Docker Host                                          │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │  bay-network (bridge)                           │  │
│  │                                                  │  │
│  │  ┌──────────┐                                   │  │
│  │  │  Bay     │ :8114 ──→ Host :8114              │  │
│  │  │ (API GW) │                                   │  │
│  │  └────┬─────┘                                   │  │
│  │       │  container_network 直连                  │  │
│  │       ├──→ Ship Pod (sandbox-xxx)               │  │
│  │       ├──→ Ship Pod (sandbox-yyy)               │  │
│  │       └──→ Gull Pod (sandbox-zzz-browser)       │  │
│  │                                                  │  │
│  └─────────────────────────────────────────────────┘  │
│                                                       │
│  bay-data volume    ← SQLite 数据库                  │
│  bay-cargos volume  ← Cargo 持久化存储               │
└──────────────────────────────────────────────────────┘
```

## 前置条件

- Docker Engine 24+ (带 Compose v2 插件)
- 镜像已通过 CD 自动推送到 GHCR（`ghcr.io/astrbotdevs/shipyard-neo-{bay,ship,gull}`）

## 快速开始

```bash
# 1. 编辑 config.yaml，搜索 CHANGE-ME 修改必须项：
#    - security.api_key → 设置强随机密钥 (e.g. `openssl rand -hex 32`)
#      （若同时设置 BAY_API_KEY 环境变量，则 BAY_API_KEY 优先）
vi config.yaml

# 2. 启动
docker compose up -d

# 3. 验证健康
curl http://localhost:8114/health

# 4. 查看日志
docker compose logs -f bay
```

## AstrBot 联合部署

使用 overlay compose 文件将 AstrBot 加入 Bay 编排：

```bash
# 启动 Bay + AstrBot
docker compose -f docker-compose.yaml -f docker-compose.with-astrbot.yaml up -d

# 查看日志
docker compose -f docker-compose.yaml -f docker-compose.with-astrbot.yaml logs -f
```

启动后：

1. **打开 Dashboard**: http://localhost:6185（默认用户名密码: astrbot）
2. **配置 Computer Use**:
   - 运行环境 → `sandbox`
   - 沙箱环境驱动器 → `shipyard_neo`
   - Endpoint → `http://bay:8114`（Docker 内部 DNS）
   - 访问令牌 → **留空**（自动从 bay-data 卷发现）
3. **保存** — 应显示 "保存成功~"

> **原理**: AstrBot 以只读方式挂载 Bay 的 `bay-data` 卷，通过 `BAY_DATA_DIR=/bay-data` 环境变量让 `_discover_bay_credentials()` 自动找到 `credentials.json` 中的 API Key。

## 文件说明

| 文件 | 说明 |
|------|------|
| `docker-compose.yaml` | Compose 编排文件，定义 Bay 服务、网络、存储卷 |
| `docker-compose.with-astrbot.yaml` | AstrBot overlay，联合部署 Bay + AstrBot |
| `config.yaml` | Bay 完整配置（profiles、driver、GC 等） |
| `README.md` | 本文档 |

## 配置

所有配置集中在 [`config.yaml`](config.yaml)，已针对生产环境优化：

- **驱动模式**: `container_network` — Bay 和 Ship/Gull 处于同一 Docker 网络，通过容器 IP 直连
- **端口映射**: 禁用 (`publish_ports: false`) — sandbox 容器不暴露宿主机端口，减少攻击面
- **认证**: 强制要求 API Key (`allow_anonymous: false`)
  - API Key 读取优先级：`BAY_API_KEY` > `security.api_key` >（首次启动且 DB 为空时）自动生成
- **GC**: 启用自动回收（包括 orphan container 检测），每 5 分钟一轮
- **Profile**: 包含 3 个常用 profile：
  - `python-default` — 标准 Python 沙箱 (1 CPU / 1GB)
  - `python-data` — 数据科学沙箱 (2 CPU / 4GB)
  - `browser-python` — 浏览器自动化 + Python 多容器沙箱

### Profile API

通过 `GET /v1/profiles` 查看可用 profile：

```bash
# 基础信息
curl -H "Authorization: Bearer <your-api-key>" http://localhost:8114/v1/profiles

# 包含 description 和容器拓扑
curl -H "Authorization: Bearer <your-api-key>" "http://localhost:8114/v1/profiles?detail=true"
```

### upload / download

`upload` 和 `download` 功能已集成在 `filesystem` capability 中，无需单独声明。
对应 API 端点为 `POST .../filesystem/upload` 和 `GET .../filesystem/download`。

## 运维

### 停止 & 清理

```bash
# 停止 Bay（不影响已运行的 sandbox 容器）
docker compose down

# 停止 Bay 并清理所有 sandbox 容器
docker compose down
docker ps -a --filter "label=bay.managed=true" -q | xargs -r docker rm -f
docker volume ls --filter "label=bay.managed=true" -q | xargs -r docker volume rm
```

### 数据备份

```bash
# 备份 SQLite 数据库
docker cp bay:/app/data/bay.db ./bay-$(date +%Y%m%d).db

# 备份 cargo 存储
docker run --rm -v bay-cargos:/data -v $(pwd):/backup \
  alpine tar czf /backup/cargos-$(date +%Y%m%d).tar.gz -C /data .
```

### 监控

Bay 提供以下端点：

- `GET /health` — 健康检查（无需认证）
- `GET /v1/admin/gc/status` — GC 状态
- `POST /v1/admin/gc/run` — 手动触发 GC

### 升级

```bash
# 拉取新镜像（GHCR 自动构建）
docker compose pull

# 重建（Bay 容器无状态，sandbox 不受影响）
docker compose up -d --force-recreate bay
```

### 使用指定版本

默认使用 `latest` 标签。如需锁定版本，编辑对应文件中的镜像 tag：

```bash
# docker-compose.yaml 中 Bay 的镜像
image: ghcr.io/astrbotdevs/shipyard-neo-bay:0.1.0

# config.yaml 中 Ship/Gull 的镜像
image: "ghcr.io/astrbotdevs/shipyard-neo-ship:0.1.0"
image: "ghcr.io/astrbotdevs/shipyard-neo-gull:0.1.0"
```

可用标签格式：`latest`、`0.1.0`（semver）、`0.1`（major.minor）、`sha-abc1234`（commit hash）。

## 镜像来源

所有镜像通过 GitHub Actions CD 自动构建并推送到 GHCR：

| 镜像 | 地址 |
|------|------|
| Bay  | `ghcr.io/astrbotdevs/shipyard-neo-bay` |
| Ship | `ghcr.io/astrbotdevs/shipyard-neo-ship` |
| Gull | `ghcr.io/astrbotdevs/shipyard-neo-gull` |

触发方式：推送 `v*` 格式 tag 或在 GitHub Actions 页面手动触发。

## 安全建议

1. **必须配置 API Key** — 建议至少设置 `security.api_key`；若同时设置 `BAY_API_KEY`，以环境变量为准
2. **使用反向代理** — 在 Bay 前部署 nginx/traefik 进行 TLS 终止
3. **限制 Docker Socket 访问** — Bay 容器需要 Docker socket，建议使用 Docker Socket Proxy
4. **网络隔离** — sandbox 容器仅通过 `bay-network` 与 Bay 通信，不暴露宿主机端口
5. **定期备份** — SQLite 数据库和 cargo 存储
