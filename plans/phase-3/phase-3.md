# Phase 3: 轻量化重构

> Bay 编排层 Go 重写，追求最小资源占用与最快启动速度。

## 概述

Phase 3 的核心目标是用 Go 重写 Bay 编排层，在保持 API 完全兼容的前提下，显著降低资源占用。

## 目标收益

| 指标 | Python 版本 | Go 版本目标 | 改善 |
|:---|:---|:---|:---|
| 内存占用 | ~150MB | ~10-30MB | **5-15x** |
| 启动时间 | ~2s | ~20-80ms | **25-100x** |
| 部署形态 | Python + 依赖 | 单一二进制 | 极简 |
| 镜像大小 | ~500MB | ~20MB | **25x** |

## 子文档索引

| 文档 | 说明 |
|:---|:---|
| [bay-go-design.md](bay-go-design.md) | Bay Go 重写详细设计 |

## 技术决策摘要

### 已确定

| 决策项 | 选型 | 理由 |
|:---|:---|:---|
| HTTP 框架 | Go 标准库 | 零依赖 |
| 数据层 | sqlc | 类型安全 + 零运行时开销 |
| Docker 交互 | Docker Go SDK | 官方支持，功能完整 |
| 数据库迁移 | golang-migrate | 成熟稳定 |

### 不做的事

| 项目 | 原因 |
|:---|:---|
| 重写 Ship | 核心依赖 IPython，保持 Python |
| 重写 SDK | 目标用户是 Python 开发者 |
| 引入新功能 | 先做到功能对等 |

## 前置条件

Phase 3 开始前需完成：

- [ ] Phase 1.5 路径安全校验
- [ ] Phase 2 GC 机制（可选，Go 版本可重新实现）
- [ ] 现有 E2E 测试稳定

## 里程碑

```
M1: 项目骨架 + 数据层
    └── sqlc 配置、schema、迁移、Repository 测试

M2: Driver 层
    └── Docker Driver 实现与测试

M3: Manager 层
    └── Sandbox/Session/Workspace Manager + 并发测试

M4: API 层
    └── HTTP 路由、认证、错误处理

M5: E2E 验证
    └── 复用现有测试、性能基准、文档更新
```

## 风险

| 风险 | 缓解措施 |
|:---|:---|
| API 不兼容 | 复用现有 E2E 测试验证 |
| 并发 bug | race detector + 完善测试 |
| 迁移失败 | 渐进式切换，影子运行验证 |

## 相关文档

- [Bay 设计文档](../bay-design.md)
- [Bay API 文档](../bay-api.md)
- [TODO.md 中的 Phase 3 规划](../../TODO.md)
