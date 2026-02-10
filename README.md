# Shipyard Neo

> 面向 AI Agents 的安全、可持久化沙箱执行平台（Secure, Persistent Execution Platform for AI Agents）

Shipyard Neo 提供“计算与存储分离”的沙箱基础设施：Agent 在隔离容器中执行 Python / Shell、读写工作区文件，并可通过独立浏览器运行时进行网页自动化。

---

## 1. 系统组成与数据流

Shipyard Neo 由控制面 **Bay** 与数据面运行时 **Ship** / **Gull** 组成：

- **Bay**：对外暴露 REST API，负责编排沙箱生命周期、鉴权、能力路由、幂等与 GC。
- **Ship**：代码运行时（Python / Shell / Filesystem / Terminal），工作目录固定为 `/workspace`。
- **Gull**：浏览器运行时，以“CLI 透传”方式执行 `agent-browser` 命令（HTTP 封装）。
- **Cargo**：持久化存储卷（Docker Volume / K8s PVC），挂载到 `/workspace`，在 Ship 与 Gull 之间共享。

整体视角参考：[`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:1) 与 [`doc/ship_architecture.md`](doc/ship_architecture.md:1)。

---

## 2. 核心概念（Domain Model）

### 2.1 Sandbox / Session / Cargo

- **Sandbox（稳定 ID）**：对外唯一计算资源单元，聚合 Profile、Cargo 与当前 Session；支持 TTL。状态由当前 Session 计算得出。详见 [`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:9)。
- **Session（临时会话）**：代表一组运行中的容器实例，可能被系统按策略回收/重建，但对 Sandbox 客户端透明。详见 [`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:18)。
- **Cargo（持久化工作区）**：存储卷，固定挂载到 `/workspace`，用于跨 Session/容器共享与持久化数据。详见 [`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:27)。

### 2.2 Profile / Capability Router

- **Profile**：定义容器拓扑、资源限制、能力集合（python/shell/filesystem/browser）与空闲回收策略。详见 [`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:51)。
- **Capability Router**：按能力类型将请求路由到提供该能力的容器与适配器（ShipAdapter / GullAdapter）。详见 [`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:82)。

---

## 3. API 入口（Bay API v1）

Bay API v1 是控制面 REST API，覆盖：

- Sandboxes：创建/查询/延长 TTL/保活/停止/删除
- Capabilities：Python 执行、Shell 执行、Filesystem 读写、Browser 透传执行
- History：执行历史查询与标注
- Skills：Candidate → Evaluate → Promote → Release → Rollback

完整参考：[`doc/bay_api_v1.md`](doc/bay_api_v1.md:1)

错误语义与错误码：[`doc/bay_error_codes.md`](doc/bay_error_codes.md:1)

---

## 4. 浏览器自动化（Gull 透传 agent-browser）

浏览器能力由 Gull 运行时提供，使用“CLI 透传”将 `agent-browser` 子命令暴露为 HTTP API。架构与实现细节见：[`doc/gull_browser_runtime.md`](doc/gull_browser_runtime.md:1)

### 4.1 最关键约定（避免 80% 的失败）

1. **传给 Gull 的 `cmd` 不需要也不应该带 `agent-browser` 前缀**。
2. Gull 会自动注入 `--session` 与 `--profile`，因此 **不要在 `cmd` 里再写** `--session` / `--profile`。
3. `cmd` **不是 shell**：不要使用 `>`, `|`, `&&`, `;` 等 shell 语法；需要落盘时，应先拿到 stdout，再通过 filesystem 写文件。
4. **ref（`@e1/@e2/...`）在页面变化后会失效**：导航/提交表单/明显 DOM 变化后必须重新 `snapshot -i`。

面向工程实践的操作指南：[`doc/agent_browser_guide.md`](doc/agent_browser_guide.md:1)

---

## 5. Ship 运行时与安全模型

Ship 运行时提供：Filesystem CRUD、IPython 执行、Shell 执行、WebSocket 终端等能力。组件与接口梳理见：[`doc/ship_architecture.md`](doc/ship_architecture.md:1)

安全要点（摘要）：

- 固定工作区根目录 `/workspace`，对路径做遍历防护（禁止逃逸）。
- 容器内采用 root + shipyard 双用户模型：root 运行服务，实际执行用户代码时降权到 `shipyard`。

---

## 6. Skills Self-Update（技能自迭代基建）

Shipyard Neo 提供的是 self-update 的“基础设施”，而不是固定训练框架：

- **执行证据层**：Python/Shell/Browser 执行自动生成并持久化 `execution_id`。
- **技能控制面**：Candidate → Evaluation → Release（canary/stable）→ Rollback。
- **多入口**：REST API / Python SDK / MCP Tools。

工程化落地指南：[`doc/skills_self_update_guide_zh.md`](doc/skills_self_update_guide_zh.md:1)

---

## 7. 部署方案

### 7.1 Docker Compose（单机生产）

面向单机生产环境的自包含部署方案，强调安全与隔离。

- **网络架构**：采用 `container_network` 驱动模式。Bay 与动态创建的 Ship/Gull 容器运行在同一 Docker Bridge 网络中，通过容器 IP 直接通信。Sandbox 容器不向宿主机暴露任何端口，极大减少了攻击面。
- **配置要点**：在 `config.yaml` 中设置 `driver.type: container_network`，并确保 `network_name` 与 Compose 网络一致。
- **快速开始**：
  ```bash
  cd deploy/docker
  docker compose up -d
  ```
- **详见**：[`deploy/docker/README.md`](deploy/docker/README.md)

### 7.2 Kubernetes（集群生产）

面向大规模集群的云原生部署方案，充分利用 K8s 的调度与存储能力。

- **资源调度**：Bay 作为 Operator 角色，通过 K8s API 动态管理 Sandbox Pod（计算）和 PersistentVolumeClaim（存储）。支持 Pod 亲和性调度与资源配额限制。
- **网络模型**：采用 Pod IP 直连模式。Bay 通过集群内 DNS 或 Pod IP 直接访问 Sandbox 实例，无缝集成 K8s 网络策略。
- **服务暴露**：Bay 服务通过 LoadBalancer 或 Ingress 暴露，支持 TLS 终结与七层路由。
- **配置要点**：在 `02-configmap.yaml` 中配置 `driver.type: k8s`，并指定用于 Cargo 动态供给的 `storage_class`。
- **快速开始**：
  ```bash
  cd deploy/k8s
  kubectl apply -f .
  ```
- **详见**：[`deploy/k8s/README.md`](deploy/k8s/README.md)

---

## 8. 仓库结构（高层视图）

- `pkgs/bay/`：Bay 控制面服务（REST API）
- `pkgs/ship/`：Ship 代码运行时
- `pkgs/gull/`：Gull 浏览器运行时（agent-browser passthrough）
- `shipyard-neo-sdk/`：Python SDK
- `shipyard-neo-mcp/`：MCP Server（面向 Agent 的工具入口）
- `deploy/`：Docker Compose / Kubernetes 部署清单
- `doc/`：本项目权威概念与专题文档
- `skills/`：Agent 技能文档（SKILL.md + references）

---

## 9. 推荐阅读路径（从 0 到能用）

1. 概念与实体关系：[`doc/bay_abstract_entities.md`](doc/bay_abstract_entities.md:1)
2. API 总览与细节：[`doc/bay_api_v1.md`](doc/bay_api_v1.md:1)
3. 错误码与排障：[`doc/bay_error_codes.md`](doc/bay_error_codes.md:1)
4. 浏览器运行时（实现与部署）：[`doc/gull_browser_runtime.md`](doc/gull_browser_runtime.md:1)
5. 浏览器操作规范（透传约束与工作流）：[`doc/agent_browser_guide.md`](doc/agent_browser_guide.md:1)
6. Ship 运行时与安全模型：[`doc/ship_architecture.md`](doc/ship_architecture.md:1)
7. self-update 闭环落地：[`doc/skills_self_update_guide_zh.md`](doc/skills_self_update_guide_zh.md:1)
