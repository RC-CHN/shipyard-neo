# Browser Runtime 集成设计

> 状态：**Draft**
> 日期：2026-02-09

## 1. 设计目标

将 `agent-browser` CLI 工具集成到 Bay 多容器架构中，实现：

1. **最小适配成本**：避免逐一封装 agent-browser 的 50+ 命令
2. **RESTful 暴露**：提供标准 HTTP API 供 CapabilityRouter 调用
3. **无缝协作**：与 Ship 容器共享 Cargo Volume

## 2. 核心问题分析

### 2.1 agent-browser 特点

| 特点 | 说明 | 挑战 |
|------|------|------|
| **CLI 驱动** | 每个命令是独立进程 | 无法直接通过 HTTP 调用 |
| **命令丰富** | 50+ 命令，涵盖导航、交互、截图等 | 逐一封装工作量大 |
| **会话管理** | `--session` 参数隔离浏览器上下文 | 需要映射到 Sandbox 概念 |
| **无状态进程** | CLI 每次调用后退出 | 需要后台服务保持浏览器运行 |
| **Refs 机制** | snapshot 返回 @e1 格式引用 | 需要在调用间保持一致 |

### 2.2 集成挑战

```
传统方案（高成本）：
┌───────────────────────────────────────────┐
│              BrowserAdapter               │
│  navigate() → POST /navigate              │
│  click()    → POST /click                 │
│  fill()     → POST /fill                  │
│  snapshot() → POST /snapshot              │
│  ...50+ 方法...                           │  ← 逐一封装，工作量大
└───────────────────────────────────────────┘
```

## 3. 推荐方案：CLI Passthrough 模式

### 3.1 核心思路

**不逐一封装命令，而是直接透传 CLI 命令字符串**

```
低适配成本方案：
┌───────────────────────────────────────────┐
│              BrowserAdapter               │
│                                           │
│  exec(cmd: str) → POST /exec              │
│      cmd = "snapshot -i"                  │
│      cmd = "click @e1"                    │
│      cmd = "fill @e2 'hello'"             │
│                                           │
│  只需实现 1 个通用接口                      │
└───────────────────────────────────────────┘
```

### 3.2 Browser Runtime 容器设计

```
┌─────────────────────────────────────────────────────────────┐
│                 Browser Runtime Container                    │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │           Thin REST Wrapper (FastAPI)                   │ │
│  │                                                         │ │
│  │  POST /exec                                             │ │
│  │    Body: {"cmd": "snapshot -i", "timeout": 30}          │ │
│  │    Response: {"stdout": "...", "stderr": "", "code": 0} │ │
│  │                                                         │ │
│  │  GET /health                                            │ │
│  │  GET /meta                                              │ │
│  └────────────────────────────────────────────────────────┘ │
│                          │                                   │
│                          ▼                                   │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              agent-browser CLI                          │ │
│  │                                                         │ │
│  │  自动注入 --session 参数（映射到 sandbox_id）            │ │
│  │  自动处理 /workspace 路径映射                           │ │
│  └────────────────────────────────────────────────────────┘ │
│                          │                                   │
│                          ▼                                   │
│              /workspace (Cargo Volume)                       │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 API 设计

#### 3.3.1 核心端点

```yaml
# Browser Runtime REST API

POST /exec:
  description: 执行 agent-browser 命令
  body:
    cmd: string       # agent-browser 命令（不含 agent-browser 前缀）
    timeout: int      # 超时秒数，默认 30
  response:
    stdout: string    # 命令输出
    stderr: string    # 错误输出
    exit_code: int    # 退出码
    
  examples:
    - cmd: "open https://example.com"
    - cmd: "snapshot -i"
    - cmd: "click @e1"
    - cmd: "fill @e2 'user@example.com'"
    - cmd: "screenshot /workspace/page.png"

GET /health:
  description: 健康检查
  response:
    status: "healthy" | "degraded" | "unhealthy"
    browser_active: boolean

GET /meta:
  description: 运行时元数据
  response:
    runtime: "browser"
    version: "1.0.0"
    capabilities:
      browser: {version: "1.0"}
      screenshot: {version: "1.0"}
      filesystem: {version: "1.0"}
```

#### 3.3.2 Browser Runtime 实现（伪代码）

```python
# browser-runtime/main.py

from fastapi import FastAPI
import subprocess
import os

app = FastAPI()

# 从环境变量获取 sandbox_id 作为 session 名
SESSION_NAME = os.environ.get("SANDBOX_ID", "default")

@app.post("/exec")
async def exec_command(cmd: str, timeout: int = 30):
    """执行 agent-browser 命令。
    
    自动注入 --session 参数，确保浏览器上下文与 Sandbox 绑定。
    """
    # 构建完整命令
    full_cmd = f"agent-browser --session {SESSION_NAME} {cmd}"
    
    try:
        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/workspace"
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "Command timed out",
            "exit_code": -1
        }

@app.get("/health")
async def health():
    # 检查浏览器进程是否存活
    result = subprocess.run(
        f"agent-browser --session {SESSION_NAME} session list",
        shell=True,
        capture_output=True
    )
    return {
        "status": "healthy" if result.returncode == 0 else "degraded",
        "browser_active": SESSION_NAME in result.stdout
    }

@app.get("/meta")
async def meta():
    return {
        "runtime": "browser",
        "version": "1.0.0",
        "capabilities": {
            "browser": {"version": "1.0"},
            "screenshot": {"version": "1.0"},
            "filesystem": {"version": "1.0"}
        }
    }
```

### 3.4 BrowserAdapter 实现

```python
# pkgs/bay/app/adapters/browser.py

from app.adapters.base import BaseAdapter, ExecutionResult

class BrowserAdapter(BaseAdapter):
    """Browser runtime 适配器。
    
    通过 CLI passthrough 模式调用 agent-browser。
    """
    
    SUPPORTED_CAPABILITIES = ["browser", "screenshot", "filesystem"]
    
    async def exec_browser(
        self,
        cmd: str,
        *,
        timeout: int = 30
    ) -> ExecutionResult:
        """执行 agent-browser 命令。
        
        Args:
            cmd: agent-browser 命令（不含 agent-browser 前缀）
            timeout: 超时秒数
            
        Returns:
            ExecutionResult 包含 stdout, stderr, exit_code
            
        Examples:
            await adapter.exec_browser("open https://example.com")
            await adapter.exec_browser("snapshot -i")
            await adapter.exec_browser("click @e1")
        """
        resp = await self._client.post(
            f"{self._endpoint}/exec",
            json={"cmd": cmd, "timeout": timeout}
        )
        data = resp.json()
        return ExecutionResult(
            stdout=data["stdout"],
            stderr=data["stderr"],
            exit_code=data["exit_code"],
            success=data["exit_code"] == 0
        )
    
    # === 便捷方法（可选，封装常用操作）===
    
    async def navigate(self, url: str) -> ExecutionResult:
        """导航到 URL。"""
        return await self.exec_browser(f"open {url}")
    
    async def snapshot(self, interactive: bool = True) -> ExecutionResult:
        """获取页面快照。"""
        flags = "-i" if interactive else ""
        return await self.exec_browser(f"snapshot {flags}")
    
    async def click(self, ref: str) -> ExecutionResult:
        """点击元素。"""
        return await self.exec_browser(f"click {ref}")
    
    async def fill(self, ref: str, text: str) -> ExecutionResult:
        """填充表单。"""
        # 转义引号
        escaped = text.replace("'", "\\'")
        return await self.exec_browser(f"fill {ref} '{escaped}'")
    
    async def screenshot(self, path: str, full_page: bool = False) -> ExecutionResult:
        """截图。"""
        flags = "--full" if full_page else ""
        return await self.exec_browser(f"screenshot {flags} {path}")
    
    # === Filesystem 能力（复用 /exec 调用 agent-browser）===
    
    async def read_file(self, path: str) -> str:
        """读取文件（通过 shell）。"""
        # Browser 容器也可以访问 /workspace
        result = await self.exec_browser(f"eval 'require(\"fs\").readFileSync(\"{path}\", \"utf-8\")'")
        if not result.success:
            raise FileNotFoundError(path)
        return result.stdout
```

## 4. Bay API 暴露

### 4.1 API 设计

在 Bay 层暴露 browser 能力，保持 RESTful 风格：

```yaml
# Bay Browser API

POST /sandboxes/{sandbox_id}/browser/exec:
  description: 执行浏览器命令
  body:
    cmd: string       # agent-browser 命令
    timeout: int      # 可选，默认 30
  response:
    stdout: string
    stderr: string
    exit_code: int
    success: boolean

# 便捷端点（可选）
POST /sandboxes/{sandbox_id}/browser/navigate:
  body:
    url: string
    
POST /sandboxes/{sandbox_id}/browser/snapshot:
  body:
    interactive: boolean  # 默认 true
    
POST /sandboxes/{sandbox_id}/browser/click:
  body:
    ref: string  # @e1 格式
```

### 4.2 CapabilityRouter 扩展

```python
# pkgs/bay/app/router/capability/capability.py

class CapabilityRouter:
    
    async def exec_browser(
        self,
        sandbox: Sandbox,
        cmd: str,
        *,
        timeout: int = 30,
    ) -> ExecutionResult:
        """执行浏览器命令。"""
        session = await self.ensure_session(sandbox)
        adapter = self._get_adapter(session, capability="browser")
        await self._require_capability(adapter, "browser")
        
        self._log.info(
            "capability.browser.exec",
            sandbox_id=sandbox.id,
            cmd=cmd[:100],
        )
        
        return await adapter.exec_browser(cmd, timeout=timeout)
```

## 5. 多容器数据结构更新

### 5.1 Session 模型扩展

```python
# pkgs/bay/app/models/session.py

class ContainerInfo(BaseModel):
    """容器运行时信息。"""
    name: str                    # ship | gull
    container_id: str            # Docker 容器 ID
    endpoint: str                # http://host:port
    runtime_type: str            # ship | gull
    capabilities: list[str]      # ["python", "shell"] | ["browser"]

class Session(SQLModel, table=True):
    """Session - 可包含多个容器。"""
    
    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    sandbox_id: str = Field(foreign_key="sandboxes.id", index=True)
    profile_id: str = Field(default="python-default")
    
    # Phase 1 兼容（单容器）
    runtime_type: str = Field(default="ship")
    container_id: Optional[str] = Field(default=None)
    endpoint: Optional[str] = Field(default=None)
    
    # Phase 2 新增（多容器）
    containers: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON)
    )
    
    def get_container_for_capability(self, capability: str) -> ContainerInfo | None:
        """根据 capability 查找容器。"""
        for c in self.containers:
            info = ContainerInfo(**c)
            if capability in info.capabilities:
                return info
        return None
```

### 5.2 Profile 配置扩展

```yaml
# config.yaml

profiles:
  - id: python-default
    description: "Python 开发环境"
    containers:
      - name: ship
        image: ship:latest
        port: 8000
        capabilities:
          - python
          - shell
          - filesystem
          - terminal

  - id: browser-python
    description: "Browser + Python 协作环境"
    containers:
      - name: ship
        image: ship:latest
        port: 8000
        capabilities:
          - python
          - shell
          - filesystem
          - terminal
        primary_for:
          - filesystem  # 文件系统操作优先走 ship
      - name: browser
        image: browser-runtime:latest
        port: 8080
        capabilities:
          - browser
          - screenshot
          - filesystem
```

## 6. 使用流程示例

### 6.1 AI Agent 工作流

```python
# AI Agent 使用 SDK 控制浏览器

from shipyard_neo import ShipyardClient

client = ShipyardClient()

# 创建带 browser 的 sandbox
sandbox = client.sandboxes.create(profile="browser-python")

# 1. 导航到页面
result = client.browser.exec(sandbox.id, "open https://example.com/login")

# 2. 获取页面快照，找到交互元素
result = client.browser.exec(sandbox.id, "snapshot -i")
# stdout: "@e1 [input type='email'], @e2 [input type='password'], @e3 [button] 'Login'"

# 3. 填写表单
client.browser.exec(sandbox.id, "fill @e1 'user@example.com'")
client.browser.exec(sandbox.id, "fill @e2 'password123'")
client.browser.exec(sandbox.id, "click @e3")

# 4. 等待导航完成
client.browser.exec(sandbox.id, "wait --load networkidle")

# 5. 截图保存到 workspace
client.browser.exec(sandbox.id, "screenshot /workspace/result.png")

# 6. 用 Python 处理截图
client.python.exec(sandbox.id, """
from PIL import Image
img = Image.open('/workspace/result.png')
print(f"Screenshot size: {img.size}")
""")
```

### 6.2 MCP Server 集成

```json
// MCP Tool Definition
{
  "name": "browser_exec",
  "description": "Execute browser automation command",
  "inputSchema": {
    "type": "object",
    "properties": {
      "sandbox_id": {"type": "string"},
      "cmd": {"type": "string", "description": "agent-browser command without prefix"}
    },
    "required": ["sandbox_id", "cmd"]
  }
}
```

## 7. 实现计划

### 7.1 Phase 2.1：Browser Runtime 容器

- [ ] 创建 `browser-runtime/` 目录结构
- [ ] 实现 Thin REST Wrapper（`/exec`, `/health`, `/meta`）
- [ ] 创建 Dockerfile（基于 `agent-browser` 官方镜像或自建）
- [ ] 本地测试 CLI passthrough

### 7.2 Phase 2.2：Bay 适配

- [ ] 实现 `BrowserAdapter`
- [ ] 扩展 `Session` 模型支持多容器
- [ ] 扩展 `ProfileConfig` 支持多容器声明
- [ ] 扩展 `DockerDriver` 创建多容器 + 共享网络

### 7.3 Phase 2.3：API 暴露

- [ ] 添加 `/sandboxes/{id}/browser/exec` 端点
- [ ] 扩展 SDK 添加 `client.browser.exec()`
- [ ] 扩展 MCP Server 添加 `browser_exec` tool

### 7.4 Phase 2.4：测试验证

- [ ] 单元测试：BrowserAdapter
- [ ] 集成测试：Browser + Ship 协作（下载文件 → Python 处理）
- [ ] E2E 测试：完整 AI Agent 工作流

## 8. 决策点

| 决策 | 选项 | 建议 | 理由 |
|------|------|------|------|
| **CLI 调用方式** | A. subprocess<br>B. asyncio.create_subprocess | B | 非阻塞，适合 FastAPI |
| **会话映射** | A. sandbox_id<br>B. session_id | A | 简单，1:1 映射 |
| **浏览器生命周期** | A. 容器启动时启动<br>B. 首次调用时启动 | B | 节省资源 |
| **错误处理** | A. 返回 exit_code<br>B. 抛出异常 | A | 让调用方决定处理策略 |

## 9. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| subprocess 开销 | 每次命令有进程创建开销 | agent-browser 内部复用浏览器进程 |
| 命令注入风险 | 安全漏洞 | 参数验证 + 沙箱隔离 |
| 浏览器内存占用 | 资源消耗大 | 设置内存限制 + idle 回收 |
| Refs 失效 | 页面变化后 @e1 不再有效 | 文档说明 + 错误提示 |
