# Bay

Bay 是 Ship 运行时的编排层，负责 Sandbox / Session / Cargo 生命周期管理，作为外部世界与 Ship 之间的唯一入口。

## 核心概念

- **Sandbox**: 对外唯一资源，聚合 Cargo + Profile + Session
- **Cargo**: 数据持久层（Docker Volume 或 K8s PVC）
- **Session**: 运行实例（Docker Container 或 K8s Pod），可回收/重建
- **Profile**: 运行时规格（镜像/资源/capabilities）

## 运行时与驱动

- **DockerDriver**: 支持 `host_port` 与 `container_network` 两种模式
- **K8sDriver**: 支持 Pod + PVC 管理、Pod IP 直连
- Bay 对上层 API 保持统一抽象，调用方无须感知具体驱动实现

## 快速开始

```bash
# 安装依赖
uv sync

# 运行开发服务器（实际入口）
uv run python -m app.main

# 运行测试
uv run pytest tests/unit -v

# 运行 K8s 测试（需 Kind）
./tests/scripts/kind/run.sh
```

## 设计文档

- [Bay 架构设计](../../plans/bay-design.md)
- [API 契约](../../plans/bay-api.md)
- [概念与职责边界](../../plans/bay-concepts.md)
- [实现路径](../../plans/bay-implementation-path.md)
- [Phase 1 进度追踪](../../plans/phase-1/progress.md)
- [Phase 2 规划](../../plans/phase-2/phase-2.md)
- [K8s Driver 分析](../../plans/phase-2/k8s-driver-analysis.md)
