# Bay Phase 2 规划：多容器与能力路由

> 状态：**Draft**
> 日期：2026-01-29

## 1. 核心目标

Phase 2 的核心目标是引入**多容器支持 (Multi-Container Support)**，同时保持**对外 API 的完全兼容性**。

用户依然通过标准的 Sandbox API 操作资源，而 Bay 内部负责将请求路由到正确的容器（如 Ship、Browser、Database 等）。

### 1.1 关键特性

1.  **透明的多容器管理**：
    - 用户创建一个 Sandbox，底层可能启动多个容器（Sidecar 模式）。
    - 所有容器共享同一个 Workspace Volume。
    - 对外表现为单一实体，API 无感知。

2.  **基于 Profile 的能力路由**：
    - 通过 Profile 定义 Sandbox 包含哪些容器。
    - 定义每个容器提供哪些 Capability（Python, Shell, Browser, Filesystem 等）。
    - 解决能力冲突（如多个容器都有 Filesystem），指定 Primary 处理者。

3.  **Session 复用优化 (可选)**：
    - 引入 Session 池化或 Keep-Warm 机制，减少冷启动时间。

## 2. 架构变更预览

### 2.1 数据模型

- **ProfileConfig**: 扩展支持多容器定义。
- **Session**: 从单 `container_id` 变为 `containers` 列表。
- **CapabilityRouter**: 增加路由逻辑，根据 Capability 查找对应容器。

### 2.2 API 兼容性

| API | 行为变化 | 对用户影响 |
|:--|:--|:--|
| `POST /sandboxes` | 根据 Profile 启动一组容器 | 无（可能启动稍慢） |
| `POST /python/exec` | 自动路由到支持 Python 的容器 | 无 |
| `POST /files/*` | 自动路由到 Primary Filesystem 容器 | 无 |
| `POST /stop` | 停止组内所有容器 | 无 |

## 3. 执行计划

1.  **设计 (Design)**
    - [ ] `multi-container-design.md`: 详细设计多容器数据结构与路由逻辑。
    - [ ] `profile-schema-update.md`: Profile 配置结构变更设计。

2.  **实现 (Implementation)**
    - [ ] 更新 `ProfileConfig` 和 `Session` 模型。
    - [ ] 改造 `DockerDriver` 支持多容器创建与网络互通。
    - [ ] 改造 `CapabilityRouter` 实现智能路由。
    - [ ] 增加 Browser 容器镜像（用于测试）。

3.  **验证 (Verification)**
    - [ ] 单元测试：路由逻辑覆盖。
    - [ ] E2E 测试：多容器协作（如 Python 控制 Browser）。

---

## 4. 多容器链路详细设计

### 4.1 典型场景：Browser + Ship 协作

**场景描述**：
- 一个 Sandbox 下有一个 Workspace
- 本次使用对应的 Session 在 Sidecar 模式下包含两个容器：Browser 和 Ship
- Browser 下载文件后，Ship 用 Python 处理该文件

#### 4.1.1 架构概览

**组件结构**：

```
Client (AI Agent / 用户)
    │
    ▼
Bay API Layer
    └── CapabilityRouter (能力路由器)
            │
            ├──[browser 请求]──► Browser Container (port 8080)
            │                         │
            └──[python/shell/fs]──► Ship Container (port 8000)
                                      │
                                      ▼
                            ┌─────────────────────┐
                            │  Workspace Volume   │
                            │    (/workspace)     │
                            └─────────────────────┘
                                      ▲
                                      │
                            Browser Container 也挂载
```

**数据流说明**：
- AI Agent 通过 Bay API 发送请求
- CapabilityRouter 根据请求的 capability 类型决定路由目标
- Browser 请求 → 路由到 Browser Container
- Python/Shell/Filesystem 请求 → 路由到 Ship Container
- **关键**：Browser 和 Ship 都挂载同一个 Workspace Volume，文件可直接共享

#### 4.1.2 文件共享流程

**步骤 1：Browser 下载文件**

```
Agent → POST /sandboxes/{id}/browser/download
        Body: {url: "https://example.com/data.pdf", save_path: "/workspace/data.pdf"}
                │
                ▼
Bay API → CapabilityRouter → 识别为 browser 能力
                │
                ▼
BrowserAdapter.download(url, save_path)
                │
                ▼
Browser Container → 下载文件 → 写入 /workspace/data.pdf
                │
                ▼
返回: {success: true, path: "/workspace/data.pdf", size: 12345}
```

**步骤 2：Ship 用 Python 处理文件**

```
Agent → POST /sandboxes/{id}/python/exec
        Body: {code: "with open('/workspace/data.pdf', 'rb') as f: ..."}
                │
                ▼
Bay API → CapabilityRouter → 识别为 python 能力
                │
                ▼
ShipAdapter.exec_python(code)
                │
                ▼
Ship Container → 读取 /workspace/data.pdf → 执行 Python 代码
                │
                ▼
返回: {success: true, output: "处理结果..."}
```

**关键点**：
- **无需文件传输**：Browser 和 Ship 共享同一个 Workspace Volume
- **路径统一**：所有文件路径都相对于 `/workspace`
- **自动路由**：CapabilityRouter 根据 capability 类型自动选择目标容器

#### 4.1.3 Browser 容器能力分析

| 能力 | 必需性 | 说明 |
|:--|:--|:--|
| `browser` | 必需 | 核心浏览器控制（导航、点击、截图、下载等） |
| `filesystem` | 必需 | 读写 /workspace 目录（处理下载文件、保存截图） |

**注意**：Browser 的 `filesystem` 能力与 Ship 的 `filesystem` 能力存在**冲突**，需要指定 Primary 处理者。

### 4.2 能力冲突与路由策略

当多个容器提供相同能力时，需要明确路由规则。

#### 4.2.1 冲突场景

| 能力 | Ship | Browser | 冲突处理建议 |
|:--|:--|:--|:--|
| `filesystem` | ✅ 完整实现 | ✅ 部分实现 | Ship 作为 Primary |
| `python` | ✅ | ❌ | Ship 独占 |
| `shell` | ✅ | ❌ | Ship 独占 |
| `browser` | ❌ | ✅ | Browser 独占 |
| `upload` | ✅ | ? | Ship 作为 Primary |
| `download` | ✅ | ✅ 浏览器下载 | 按 context 路由 |

#### 4.2.2 路由规则设计（待定）

**方案 A：Profile 静态声明 Primary**

```yaml
profiles:
  - id: browser-python
    containers:
      - name: ship
        image: ship:latest
        capabilities:
          - python          # 独占
          - shell           # 独占
          - filesystem      # primary: true
          - upload
          - terminal
        primary_for:
          - filesystem      # 冲突能力的默认处理者
      - name: browser
        image: browser:latest
        capabilities:
          - browser         # 独占
          - filesystem      # 可用，但非 primary
```

**方案 B：Capability 子类型区分**

```yaml
# 将 download 细分为不同子类型
capabilities:
  - download.file       # 直接下载文件 → Ship
  - download.browser    # 浏览器内下载 → Browser
```

**方案 C：请求级显式指定**

```json
// 请求时指定 target container
{
  "capability": "filesystem",
  "target": "browser",  // 可选，不指定则用 primary
  "action": "read",
  "path": "/workspace/file.txt"
}
```

### 4.3 Session 模型扩展

#### 4.3.1 当前模型（Phase 1 - 单容器）

```python
class Session:
    id: str
    sandbox_id: str
    container_id: Optional[str]  # 单容器
    endpoint: Optional[str]       # 单端点
    runtime_type: str             # ship
```

#### 4.3.2 目标模型（Phase 2 - 多容器）

```python
class ContainerInfo:
    name: str           # ship | browser | custom
    container_id: str
    endpoint: str       # http://host:port
    capabilities: list[str]  # 该容器提供的能力

class Session:
    id: str
    sandbox_id: str
    containers: list[ContainerInfo]  # 多容器
    primary_container: str           # 默认容器名
```

#### 4.3.3 CapabilityRouter 扩展

```python
class CapabilityRouter:
    def _get_adapter(self, session: Session, capability: str) -> BaseAdapter:
        """根据 capability 查找对应容器的 Adapter。
        
        Args:
            session: 包含多容器信息的 Session
            capability: 需要的能力名称
            
        Returns:
            对应容器的 Adapter
            
        Raises:
            CapabilityNotSupportedError: 无容器支持该能力
            AmbiguousCapabilityError: 多容器支持但未指定 primary
        """
        # 1. 查找提供该 capability 的容器
        candidates = [c for c in session.containers if capability in c.capabilities]
        
        if not candidates:
            raise CapabilityNotSupportedError(capability)
        
        if len(candidates) == 1:
            return self._create_adapter(candidates[0])
        
        # 2. 多个候选，使用 primary
        primary = next((c for c in candidates if c.name == session.primary_container), None)
        if primary:
            return self._create_adapter(primary)
        
        # 3. 无 primary，按 Profile 配置的 primary_for 决定
        # ... 待实现
```

### 4.4 BrowserAdapter 设计草案

```python
class BrowserAdapter(BaseAdapter):
    """Browser runtime 适配器。
    
    支持的能力：browser, filesystem
    """

    SUPPORTED_CAPABILITIES = [
        "browser",
        "filesystem",
    ]

    # -- Browser 能力 --
    async def navigate(self, url: str) -> BrowserResult:
        """导航到指定 URL。"""
        ...

    async def screenshot(self, path: str) -> None:
        """截图并保存到 workspace。"""
        ...

    async def click(self, selector: str) -> BrowserResult:
        """点击页面元素。"""
        ...

    async def download(self, url: str, save_path: str) -> DownloadResult:
        """下载文件到 workspace。
        
        Args:
            url: 下载链接
            save_path: 保存路径（相对于 /workspace）
            
        Returns:
            下载结果，包含文件路径和大小
        """
        ...

    async def get_content(self, selector: str) -> str:
        """获取页面元素内容。"""
        ...

    # -- Filesystem 能力（共享） --
    async def read_file(self, path: str) -> str:
        ...

    async def write_file(self, path: str, content: str) -> None:
        ...
```

---

## 5. 待敲定的设计决策

### 5.1 能力路由

| 决策点 | 选项 | 当前倾向 | 备注 |
|:--|:--|:--|:--|
| **filesystem 冲突处理** | A. Profile 静态 Primary<br/>B. 子类型区分<br/>C. 请求级指定 | A | 最简单，覆盖大多数场景 |
| **download 语义** | A. filesystem.download（通用）<br/>B. browser.download（浏览器特有） | B | 浏览器下载是特殊行为 |
| **Primary 声明位置** | A. Profile 配置<br/>B. Adapter 代码 | A | 可配置更灵活 |

### 5.2 数据模型

| 决策点 | 选项 | 当前倾向 | 备注 |
|:--|:--|:--|:--|
| **Session 容器存储** | A. containers JSON 字段<br/>B. 独立 Container 表 | A | 简单，Phase 2 足够 |
| **容器间网络** | A. 共享 localhost<br/>B. Docker network + DNS | B | 更隔离，易扩展 |

### 5.3 API 设计

| 决策点 | 选项 | 当前倾向 | 备注 |
|:--|:--|:--|:--|
| **Browser API 路径** | A. /browser/navigate<br/>B. /capabilities/browser/navigate | A | 与现有 /python/exec 一致 |
| **Browser 下载 API** | A. POST /browser/download<br/>B. 通过 navigate + 等待下载事件 | A | 显式更可控 |

### 5.4 实现细节

| 决策点 | 选项 | 当前倾向 | 备注 |
|:--|:--|:--|:--|
| **Browser 镜像** | A. 自研（基于 Playwright）<br/>B. 集成现有（如 browserless） | ? | 需评估 |
| **容器启动顺序** | A. 并行启动<br/>B. Ship 先于 Browser | A | 减少冷启动时间 |
| **健康检查** | A. 全部容器健康才 Ready<br/>B. 任一容器健康即可用 | A | 保证完整能力 |

---

## 6. 下一步行动

1. **确认上述决策点** - 逐项讨论并敲定
2. **设计 Profile Schema** - 支持多容器声明
3. **实现 ContainerInfo 模型** - 扩展 Session
4. **实现 BrowserAdapter** - 参考 ShipAdapter
5. **扩展 CapabilityRouter** - 支持多容器路由
