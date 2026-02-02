# Bay

Bay 是 Ship 容器的编排层，负责容器生命周期管理，作为外部世界与 Ship 之间的唯一入口。

## 核心概念

- **Sandbox**: 对外唯一资源，聚合 Cargo + Profile + Session
- **Cargo**: 数据持久层（Docker Volume / K8s PVC）
- **Session**: 运行实例（容器/Pod），可回收/重建
- **Profile**: 运行时规格（镜像/资源/capabilities）

## 快速开始

```bash
# 安装依赖
uv sync

# 运行开发服务器
uv run python -m bay.main

# 运行测试
uv run pytest
```

## 设计文档

- [Bay 架构设计](../../plans/bay-design.md)
- [API 契约](../../plans/bay-api.md)
- [概念与职责边界](../../plans/bay-concepts.md)
- [实现路径](../../plans/bay-implementation-path.md)
