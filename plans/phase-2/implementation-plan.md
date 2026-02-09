# Phase 2 实施计划：多容器架构与浏览器支持

> **状态**：已批准
> **日期**：2026-02-09
> **目标**：实现多容器 Sandbox（Ship + Browser）架构，支持智能能力路由，同时保持向后兼容性。

---

## 1. 概述 (Overview)

Phase 2 将 Bay 从单容器架构转变为**多容器架构**。这一转变使得 Bay 能够支持更复杂的场景，例如"使用 Python 数据分析环境控制无头浏览器"，其中 Python 环境（Ship）和浏览器环境（Browser Runtime）作为独立的容器运行，但共享同一个 Cargo 工作区和网络环境。

### 核心特性
1.  **多容器支持**：一个 Sandbox 可以运行多个容器（如 Ship + Browser），它们共享 Docker 网络和 Cargo 卷。
2.  **能力路由**：API 请求（如 `/python/exec`, `/browser/exec`）会根据配置自动路由到正确的容器。
3.  **浏览器集成**：通过 `agent-browser` CLI 透传模式提供原生浏览器自动化支持。
4.  **向后兼容**：现有的单容器 Profile 和客户端无需修改即可继续工作。

---

## 2. 架构设计 (Architecture Design)

### 2.1 多容器拓扑

```mermaid
graph TD
    subgraph "Docker Host"
        subgraph "Session Network (Bridge)"
            Ship[Ship Container\n(Python/Shell)]
            Browser[Browser Container\n(agent-browser)]
        end
        
        CargoVol[(Cargo Volume)]
        
        Ship -- Mount --> CargoVol
        Browser -- Mount --> CargoVol
        
        Ship -- HTTP --> Browser
    end
    
    Bay[Bay Service] -- HTTP (Host Port) --> Ship
    Bay[Bay Service] -- HTTP (Host Port) --> Browser
```

### 2.2 核心组件交互

1.  **DockerDriver**：负责创建会话级网络，并行启动多个容器，挂载共享卷。
2.  **SessionManager**：聚合多个容器的状态，管理 Session 的整体生命周期。
3.  **CapabilityRouter**：根据 Capability 将请求分发到对应的容器适配器（Adapter）。

---

## 3. 详细实施步骤 (Detailed Implementation Steps)

### 阶段 2.1：数据模型与配置 (Data Model & Configuration)

此阶段建立多容器的基础数据结构，必须最先完成。

#### 任务清单
- [ ] **重构 `ProfileConfig` (`pkgs/bay/app/config.py`)**
    - 新增 `ContainerSpec` 模型：
        ```python
        class ContainerSpec(BaseModel):
            name: str                    # 容器名称 (e.g., "ship", "browser")
            image: str                   # 镜像地址
            runtime_type: str            # 运行时类型 (e.g., "ship", "browser")
            runtime_port: int            # 容器内端口
            resources: ResourceSpec      # 资源限制
            capabilities: list[str]      # 提供的能力
            primary_for: list[str]       # 主处理能力列表 (用于冲突解决)
            env: dict[str, str]          # 环境变量
        ```
    - 修改 `ProfileConfig`：
        - 添加 `containers: list[ContainerSpec]` 字段。
        - 添加 `startup: StartupConfig` 字段（定义启动顺序）。
        - 实现 `model_validator(mode="before")`：将旧的 `image`, `runtime_type` 等字段自动转换为包含单个 `ContainerSpec` 的 `containers` 列表。
- [ ] **更新 `Session` 模型 (`pkgs/bay/app/models/session.py`)**
    - 添加 `containers` JSON 列：
        ```python
        # 存储运行时容器状态
        # 格式: [{name, container_id, endpoint, status, capabilities, runtime_type}, ...]
        containers: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
        ```
    - 废弃/兼容 `container_id` 和 `endpoint` 字段（保留用于向后兼容，指向主容器）。
- [ ] **编写单元测试**
    - 验证旧版 Profile 配置能正确加载并转换为多容器格式。
    - 验证新版多容器 Profile 配置能正确解析。

### 阶段 2.2：Docker 驱动演进 (Docker Driver Evolution)

此阶段是核心逻辑变更最复杂的部分，涉及网络管理和多容器编排。

#### 任务清单
- [ ] **实现网络管理 (`DockerDriver`)**
    - `create_session_network(session_id)`: 创建 `bay_net_{session_id}`。
    - `remove_session_network(session_id)`: 清理网络。
- [ ] **重构容器创建逻辑 (`create_session`)**
    - 遍历 `profile.containers`。
    - 为每个容器调用 `docker.create`：
        - 加入 Session 网络，设置 `aliases=[container.name]`（实现容器间通过 host name 互访）。
        - 挂载同一个 Cargo Volume 到 `/workspace`。
        - 注入环境变量（`BAY_SESSION_ID`, `BAY_CONTAINER_NAME` 等）。
- [ ] **重构容器启动与停止 (`start_session`, `stop_session`)**
    - 并行启动所有容器。
    - 收集所有容器的 endpoint。
    - 停止时，确保所有容器被停止并删除，最后删除网络。
- [ ] **更新 `SessionManager`**
    - 状态聚合逻辑：只有所有容器都 `RUNNING`，Session 才算 `RUNNING`。
    - 只要有一个容器 `FAILED`，Session 标记为 `FAILED`。

### 阶段 2.3：能力路由 (Capability Routing)

此阶段实现智能请求分发。

#### 任务清单
- [ ] **更新 `CapabilityRouter` (`pkgs/bay/app/router/capability/capability.py`)**
    - 实现路由算法：
        ```python
        def get_target_container(self, profile, capability):
            # 1. 检查 primary_for
            # 2. 检查 capabilities 列表
            # 3. 返回匹配的 ContainerSpec
        ```
    - 更新 `_get_adapter`：
        - 根据路由结果找到目标容器。
        - 从 `Session.containers` 中获取该容器的实际 endpoint。
        - 根据 `runtime_type` 实例化对应的 Adapter（目前支持 `ship`，即将支持 `browser`）。
- [ ] **验证测试**
    - Mock Session 和 Profile，测试 `python` 能力路由到 Ship，`browser` 能力路由到 Browser。

### 阶段 2.4：浏览器运行时与适配器 (Browser Runtime & Adapter)

此阶段开发新的运行时组件。

#### 任务清单
- [ ] **构建 `browser-runtime` 镜像**
    - 创建 `pkgs/bay-browser/Dockerfile`。
    - 基础镜像：Python 3.11 Slim。
    - 安装依赖：`agent-browser`, `playwright`, `fastapi`, `uvicorn`。
    - 预装 Playwright 浏览器：`RUN playwright install chromium`。
- [ ] **开发 HTTP Wrapper (`pkgs/bay-browser/app/main.py`)**
    - `POST /exec`: 接收 `cmd`，通过 `subprocess` 调用 `agent-browser`。
    - 自动注入 `--session {SANDBOX_ID}` 参数以隔离浏览器上下文。
    - `GET /health`: 检查进程状态。
    - `GET /meta`: 返回 Capabilities 声明。
- [ ] **实现 `BrowserAdapter` (`pkgs/bay/app/adapters/browser.py`)**
    - 继承 `BaseAdapter`。
    - 实现 `exec_browser(cmd)` 方法。
    - 实现 `screenshot`, `navigate` 等便捷方法的封装。

### 阶段 2.5：API 与 SDK (API & SDK)

此阶段对外暴露新能力。

#### 任务清单
- [ ] **新增 Bay API 端点**
    - `POST /sandboxes/{id}/browser/exec`: 执行浏览器命令。
    - (可选) `POST /sandboxes/{id}/browser/navigate`。
- [ ] **更新 Python SDK**
    - 在 `Sandbox` 对象上添加 `browser` 属性。
    - 支持 `sandbox.browser.exec("open https://example.com")`。
- [ ] **集成测试**
    - 编写 E2E 测试：创建一个包含 Browser 的 Sandbox，执行导航和截图，并验证 Python 容器能读取截图文件。

---

## 4. 验证与测试 (Verification & Testing)

### 4.1 单元测试
- **Config**: 确保 V1 Profile 自动转换为 V2 格式，字段映射正确。
- **Routing**: 测试多容器场景下的能力冲突解决逻辑（`primary_for` 优先级）。

### 4.2 集成测试
- **Docker Lifecycle**:
    1. 创建 Session。
    2. 验证 Docker 中存在 2 个容器和 1 个网络。
    3. 验证两个容器都挂载了同一个 Volume。
    4. 销毁 Session，验证资源（容器、网络）全被清理。
- **Connectivity**:
    1. 在 Ship 容器中 `curl http://browser:8080`，验证容器间网络互通。

### 4.3 端到端 (E2E) 场景
**场景：自动化网页数据抓取**
1. 用户创建一个 `browser-python` 类型的 Sandbox。
2. 调用 Browser API: `open https://news.ycombinator.com`。
3. 调用 Browser API: `screenshot /workspace/hn.png`。
4. 调用 Python API: 检查 `/workspace/hn.png` 是否存在，并使用 PIL 读取图片尺寸。
5. 验证成功。

---

## 5. 风险管理 (Risk Management)

| 风险点 | 影响 | 缓解措施 |
|--------|------|----------|
| **启动延迟** | 多容器并行启动可能导致 Session 准备时间变长 | 采用 `asyncio.gather` 并行启动；优化镜像大小。 |
| **资源竞争** | Browser 容器内存消耗大，可能导致 OOM | 在 Profile 中为 Browser 设置较高的内存限制（如 2GB），并配置 Docker 资源限制。 |
| **僵尸进程** | `agent-browser` CLI 可能在超时后残留 | HTTP Wrapper 需实现严格的超时控制和子进程清理（使用 `subprocess.run(timeout=...)`）。 |
| **网络残留** | 异常崩溃可能导致 Docker Network 未删除 | 在 `DockerDriver` 启动时增加清理孤儿网络的逻辑；GC 任务增加网络清理。 |
