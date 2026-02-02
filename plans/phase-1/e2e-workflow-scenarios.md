# E2E Workflow Scenarios

本文档规划了用于端到端测试的真实用户组合工作流场景。这些场景旨在模拟用户从创建沙箱到最终销毁的完整交互过程，覆盖核心功能和边界情况。

---

## 场景 1: 交互式数据分析 (Jupyter 风格)

**用户画像**: 数据分析师，探索数据，快速试验。

**目标**: 验证多轮代码执行、文件上传/下载、以及 stop/resume 后的数据持久化。

### 架构行为说明

当调用 `stop` 时：
1.  **容器被停止** (`docker stop`)
2.  IPython Kernel 终止 → **变量丢失**
3.  Sandbox 状态变为 `idle`, `current_session_id` 设为 `None`
4.  **Workspace Volume 保留** → 文件仍在

当 stop 后再次执行代码时：
1.  `ensure_running()` 发现 `current_session_id is None`
2.  **创建新的 Session 记录**
3.  **创建新的 Docker 容器** (挂载同一 Workspace Volume)
4.  新的 IPython Kernel 启动

> **设计意图**: `stop` 意味着"释放计算资源"，每次 resume 都是全新的容器。这提供了干净的运行环境，避免旧容器的潜在状态问题。文件通过 Workspace Volume 持久化，变量/内存状态不持久化。

### 行为序列

1.  **创建沙箱**: `POST /v1/sandboxes` -> 获取 `sandbox_id`, 状态 `idle`
2.  **上传数据文件**: `POST /filesystem/upload` 上传 `sales.csv`
3.  **执行代码 (第1轮)**: `POST /python/exec` - `import pandas as pd; df = pd.read_csv('sales.csv'); df.head()` -> 返回表格前几行
4.  **执行代码 (第2轮)**: `POST /python/exec` - `df['revenue'].sum()` -> 返回总和 (变量 `df` 在同一 Session 内保持)
5.  **执行代码 (第3轮)**: `POST /python/exec` - `import matplotlib.pyplot as plt; df['revenue'].plot(); plt.savefig('chart.png')` -> 生成图片文件
6.  **下载结果文件**: `GET /filesystem/download?path=chart.png` -> 获取 PNG 图片二进制内容
7.  **停止沙箱**: `POST /sandboxes/{id}/stop` -> 状态变为 `idle`，容器停止，Kernel 终止，但 Workspace Volume 保留
8.  **(用户离开几小时...)**
9.  **恢复执行 (尝试访问旧变量)**: `POST /python/exec` - `df.head()` -> **失败**: `NameError: name 'df' is not defined` (新容器，新 Kernel，变量丢失)
10. **恢复执行 (重新加载数据)**: `POST /python/exec` - `import pandas as pd; df = pd.read_csv('sales.csv'); df.head()` -> **成功**: 文件 `sales.csv` 仍然存在
11. **验证持久化文件**: `GET /filesystem/download?path=chart.png` -> **成功**: 之前生成的图片仍然存在
12. **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204 No Content

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 多轮 `python/exec` | 变量在同一 Session 内保持 (步骤 3, 4) |
| 2 | `stop` 后 `resume` | 新容器、新 Kernel，变量丢失 (步骤 9) |
| 3 | 文件持久化 | `sales.csv` 和 `chart.png` 在 `stop/resume` 后仍然存在 (步骤 10, 11) |
| 4 | `download` 二进制文件 | 能正确下载图片文件 (步骤 6, 11) |


---

## 场景 2: 脚本开发与调试 (IDE 风格)

**用户画像**: 开发者，编写和调试 Python 脚本。

**目标**: 验证文件的创建、修改、覆盖写入，以及执行失败时的错误处理。

### 架构行为说明

1.  **首次文件操作触发容器启动**: `PUT /filesystem/files` 会调用 `ensure_running()` 启动容器
2.  **文件覆盖**: 使用 `mode="w"`，同路径文件会被完全覆盖
3.  **执行失败时**: `success=false`, `error` 字段包含完整的 Python traceback

### 行为序列

1.  **创建沙箱**: `POST /v1/sandboxes` -> 获取 `sandbox_id`
2.  **写入脚本 (版本1, 有 Bug)**: `PUT /filesystem/files` - path: `script.py`, content: `print(1/0)` (故意除零错误)
    - 首次文件操作触发容器启动
3.  **执行脚本**: `POST /python/exec` - `exec(open('script.py').read())` 
    - 返回 `success=false`
    - `error` 包含 `ZeroDivisionError: division by zero` traceback
    - `output` 为空
4.  **修改脚本 (版本2, 修复 Bug)**: `PUT /filesystem/files` - path: `script.py`, content: `print('Hello, World!')` (覆盖写入)
5.  **执行脚本**: `POST /python/exec` - `exec(open('script.py').read())` 
    - 返回 `success=true`
    - `output`: `Hello, World!\n`
6.  **读取脚本内容**: `GET /filesystem/files?path=script.py` -> 返回版本2的内容
7.  **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 文件覆盖写入 | `PUT` 同一路径会更新文件内容 (步骤 4) |
| 2 | 执行失败 | `python/exec` 返回 `success=false`, `error` 包含 traceback (步骤 3) |
| 3 | 执行成功后输出 | `output` 包含 print 输出 (步骤 5) |
| 4 | 读取文件 | `GET /filesystem/files` 返回最新内容 (步骤 6) |

---

## 场景 3: 项目初始化与依赖安装 (代码仓库风格)

**用户画像**: 软件工程师，设置一个项目环境。

**目标**: 验证多文件/嵌套目录创建，以及**依赖安装的持久化边界**。

### 架构行为说明

1.  **嵌套目录自动创建**: Ship 的 `/fs/write_file` 会自动创建父目录 (`mkdir -p` 语义)
2.  **依赖持久化边界**:
    - **持久化**: 安装到 `/workspace/` 内 (如 `pip install --target /workspace/.libs`)
    - **不持久化**: 标准 `pip install` 安装到容器系统目录，stop 后丢失
3.  **新容器挂载同一 Volume**: stop/resume 后，新容器挂载相同的 Workspace Volume，文件和 `--target` 安装的库都保留

### 行为序列

1.  **创建沙箱**: `POST /v1/sandboxes` -> 获取 `sandbox_id`
2.  **写入依赖文件**: `PUT /filesystem/files` - path: `requirements.txt`, content: `requests==2.31.0`
3.  **写入代码到嵌套目录**: `PUT /filesystem/files` - path: `src/main.py`, content: `import requests; print(requests.__version__)`
    - Ship 自动创建 `src/` 目录
4.  **安装依赖到 Workspace 内**: `POST /python/exec` - `import subprocess; subprocess.run(['pip', 'install', '-r', 'requirements.txt', '--target', '/workspace/.libs'], check=True)`
    - 库安装到 `/workspace/.libs/` (Volume 内，会持久化)
5.  **执行代码 (使用安装的库)**: `POST /python/exec` - `import sys; sys.path.insert(0, '/workspace/.libs'); exec(open('src/main.py').read())`
    - 输出 `2.31.0`
6.  **停止沙箱**: `POST /sandboxes/{id}/stop`
    - 容器停止，Kernel 终止
    - Workspace Volume 保留 (包含 `requirements.txt`, `src/main.py`, `.libs/`)
7.  **恢复执行 (验证依赖持久化)**: `POST /python/exec` - `import sys; sys.path.insert(0, '/workspace/.libs'); import requests; print(requests.__version__)`
    - **新容器**挂载同一 Workspace Volume
    - `.libs/` 仍存在
    - 输出 `2.31.0`
8.  **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 嵌套目录 | `PUT` 到 `src/main.py` 时自动创建 `src/` 目录 (步骤 3) |
| 2 | 依赖持久化 (使用 `--target`) | `pip install --target /workspace/.libs` 安装的库在 stop/resume 后仍可用 (步骤 7) |

> **重要**: 标准 `pip install` 安装到容器系统目录，**不会持久化**。如果需要持久化依赖，必须使用 `--target /workspace/.libs` 或在 `/workspace` 内创建虚拟环境。

---

## 场景 4: 简单快速执行 (无状态 Serverless 风格)

**用户画像**: 用户（或自动化系统）只想快速执行一段代码，不关心持久化。

**目标**: 验证最小路径下的创建->执行->删除流程。

### 架构行为说明

1.  **懒加载**: `POST /v1/sandboxes` 只创建 Sandbox 记录和 Workspace Volume，**不启动容器**
2.  **冷启动**: 首次 `python/exec` 触发完整启动流程:
    - 创建 Session → 创建容器 → 启动容器 → 等待 Ship 就绪 → 执行代码
3.  **冷启动延迟**: 
    - 镜像已缓存: 2-5 秒
    - 需要拉取镜像: 30-120+ 秒
4.  **完全清理**: `DELETE` 会删除容器和 Volume

### 行为序列

1.  **创建沙箱**: `POST /v1/sandboxes` -> 获取 `sandbox_id`
    - 状态: `idle`
    - **容器未启动**
2.  **执行代码**: `POST /python/exec` - `print(2 * 21)`
    - 触发 `ensure_running()` → 创建并启动容器
    - 等待 Ship 就绪 (冷启动延迟)
    - 执行代码
    - 返回 `success=true`, output: `42\n`
3.  **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204
    - 容器被删除
    - Workspace Volume 被删除
    - 后续 GET 返回 404

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 最小路径 | 3 个 API 调用完成完整生命周期 |
| 2 | 懒加载 | 创建时不启动容器，执行时才启动 |
| 3 | 冷启动 | 首次执行有延迟，但应在合理时间内完成 |
| 4 | 完全清理 | 删除后 GET 返回 404 |

---

## 场景 5: 长任务续命（extend_ttl，长跑 Job 风格）

**用户画像**: 需要跑长时间任务的开发者/数据工程师（训练、ETL、批量推理）。

**背景痛点**: 创建 sandbox 时 TTL 估算过短（或任务超预期），导致任务进行中 sandbox 可能在 TTL 到期后进入 `expired`，后续任何 `ensure_running` 路径都应视为不可恢复。

**目标**: 验证 `POST /v1/sandboxes/{id}/extend_ttl` 的真实工作流语义：
- 在未过期前成功续命，使长任务可持续运行/可继续触发 `python/exec`
- 客户端重试通过 `Idempotency-Key` 回放同一响应，避免“多续一次”
- TTL 到期后拒绝续命（不可复活）

### 架构行为说明

1. **extend_ttl 只影响 `expires_at`**：不改变 `idle_expires_at`，也不隐式启动或停止容器。
2. **不可复活**：当 `expires_at < now` 时，`extend_ttl` 返回 `409 sandbox_expired`。
3. **防御性基准**：服务端按 `new = max(old, now) + extend_by` 计算，抵抗边界抖动。
4. **幂等语义**：同一请求重试（相同 body + `Idempotency-Key`）回放同一响应体（包含相同 `expires_at`）。

### 行为序列

1. **创建沙箱（短 TTL）**: `POST /v1/sandboxes`，body: `{ "profile": "python-default", "ttl": 120 }`
   - 预期返回 `expires_at` 距当前约 120s。
2. **启动并执行长任务（模拟）**: `POST /python/exec`
   - 例如：`import time; print('start'); time.sleep(5); print('done')`
   - 目的：覆盖“任务执行期间 TTL 可能不足”的语境。
3. **接近过期前续命**: `POST /v1/sandboxes/{id}/extend_ttl`，body: `{ "extend_by": 600 }`
   - 建议客户端携带 `Idempotency-Key`（例如 `ttl-extend-<uuid>`）。
   - 预期：200，返回更新后的 `expires_at`。
4. **模拟网络重试（同 Key 同 Body）**: 再次调用 `extend_ttl`（相同 `Idempotency-Key` 与 body）
   - 预期：200，**响应体完全一致**（`expires_at` 不再变化）。
5. **继续执行任务/后续操作**: 再次 `POST /python/exec`（例如打印时间戳）
   - 预期：可继续运行（TTL 未过期）。
6. **等待直到 TTL 过期**（测试环境可通过缩短 TTL/睡眠实现）。
7. **过期后尝试续命**: `POST /v1/sandboxes/{id}/extend_ttl`，body: `{ "extend_by": 60 }`
   - 预期：`409 sandbox_expired`（不可复活）。
8. **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204。

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 续命成功 | 未过期时 `extend_ttl` 更新 `expires_at`（步骤 3） |
| 2 | 幂等回放 | 同 `Idempotency-Key` 重试返回完全相同的 body（步骤 4） |
| 3 | 不可复活 | 过期后 `extend_ttl` 返回 `409 sandbox_expired`（步骤 7） |
| 4 | 续命不影响算力状态 | `extend_ttl` 不应改变 session/容器的启停语义（通过步骤 5 侧面验证） |

---

## 场景 6: AI Agent 代码生成与迭代修复 (Agentic Coding 风格)

**用户画像**: LLM-based coding agent（如 Cursor、Kilo Code、Devin），需要在沙箱中执行生成的代码、根据错误反馈迭代修复。

**背景痛点**: Agent 生成的代码首次执行可能失败（语法错误、逻辑错误、缺少依赖），需要多轮"执行→分析错误→修复→重试"循环。任务可能超预期耗时，需要动态续命 TTL。

**目标**: 验证 AI Agent 典型工作流：
- 创建沙箱并设置合理初始 TTL
- 多轮代码执行与错误处理
- 根据 traceback 迭代修复
- 任务耗时超预期时 `extend_ttl` 续命
- 幂等保护防止重复请求

### 架构行为说明

1.  **Agent 友好的错误响应**: `python/exec` 的 `success=false` 时，`error` 字段包含完整 Python traceback，供 Agent 解析后修复代码。
2.  **多轮执行变量共享**: 在同一 Session 内，前一轮定义的变量/函数在后续轮次可用，支持增量开发。
3.  **TTL 续命时机**: Agent 应在接近 `expires_at` 时主动 `extend_ttl`，而非等到过期。建议 Agent 实现"任务开始时记录 TTL，每次执行前检查剩余时间"的策略。
4.  **幂等保护**: Agent 网络不稳定时可能重试同一请求，`Idempotency-Key` 确保不会重复执行副作用操作（如 `extend_ttl` 不会多续时间）。

### 行为序列

1.  **创建沙箱（短 TTL 模拟紧迫场景）**: `POST /v1/sandboxes`
    - body: `{ "profile": "python-default", "ttl": 300 }`
    - Header: `Idempotency-Key: agent-task-001-create`
    - 预期：返回 `sandbox_id`，`expires_at` 距当前约 300s

2.  **Agent 生成代码（版本1，有 Bug）**: `PUT /filesystem/files`
    - path: `solution.py`
    - content:
      ```python
      def calculate_fibonacci(n):
          if n <= 1:
              return n
          return calculate_fibonacci(n-1) + calculate_fibonaci(n-2)  # typo: fibonaci
      
      print(calculate_fibonacci(10))
      ```
    - 首次文件操作触发容器启动

3.  **执行代码（第1轮，失败）**: `POST /python/exec`
    - code: `exec(open('solution.py').read())`
    - 预期：`success=false`
    - `error` 包含 `NameError: name 'calculate_fibonaci' is not defined`

4.  **Agent 解析错误并修复**: `PUT /filesystem/files`
    - path: `solution.py`
    - content（修复 typo）:
      ```python
      def calculate_fibonacci(n):
          if n <= 1:
              return n
          return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)
      
      print(calculate_fibonacci(10))
      ```

5.  **执行代码（第2轮，成功但慢）**: `POST /python/exec`
    - code: `exec(open('solution.py').read())`
    - 预期：`success=true`，`output`: `55\n`
    - 假设此时 Agent 发现任务比预期复杂，需要更多时间

6.  **检查 TTL 剩余并续命**: Agent 发现剩余 TTL 不足，调用 `POST /v1/sandboxes/{id}/extend_ttl`
    - body: `{ "extend_by": 600 }`
    - Header: `Idempotency-Key: agent-task-001-extend-1`
    - 预期：200，`expires_at` 延长 600s

7.  **模拟网络重试（同 Key 同 Body）**: Agent 网络超时，重试同一 `extend_ttl` 请求
    - 相同 `Idempotency-Key` 与 body
    - 预期：200，**响应体完全一致**（`expires_at` 不再变化）

8.  **Agent 继续开发，优化代码**: `PUT /filesystem/files`
    - path: `solution.py`
    - content（优化版，使用缓存）:
      ```python
      from functools import lru_cache
      
      @lru_cache(maxsize=None)
      def calculate_fibonacci(n):
          if n <= 1:
              return n
          return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)
      
      # 测试更大的数
      for i in [10, 50, 100]:
          print(f"fib({i}) = {calculate_fibonacci(i)}")
      ```

9.  **执行优化后代码**: `POST /python/exec`
    - code: `exec(open('solution.py').read())`
    - 预期：`success=true`
    - `output` 包含 `fib(10) = 55`, `fib(50) = ...`, `fib(100) = ...`

10. **验证变量在 Session 内共享**: `POST /python/exec`
    - code: `print(calculate_fibonacci(200))` （使用之前定义的函数）
    - 预期：`success=true`，直接使用前一轮定义的 `calculate_fibonacci`

11. **Agent 任务完成，下载结果**: `GET /filesystem/files?path=solution.py`
    - 预期：返回最终优化版代码

12. **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 错误信息可用于调试 | `python/exec` 失败时 `error` 包含完整 traceback（步骤 3） |
| 2 | 代码修复后重试成功 | 覆盖写入 `solution.py` 后执行成功（步骤 5） |
| 3 | extend_ttl 成功 | 未过期时续命成功，`expires_at` 更新（步骤 6） |
| 4 | 幂等回放 | 同 `Idempotency-Key` 重试返回相同响应（步骤 7） |
| 5 | 变量跨轮次共享 | 同 Session 内函数定义可复用（步骤 10） |
| 6 | Agent 创建幂等 | 创建时使用 `Idempotency-Key` 防止重复创建（步骤 1） |

### Agent 最佳实践建议

> **TTL 管理策略**: Agent 应在任务开始时记录 `expires_at`，在每次主要操作前检查剩余时间。当剩余时间 < 预估操作时间 + 缓冲（如 60s）时，主动 `extend_ttl`。

> **幂等键策略**: 建议格式 `{task_id}-{operation}-{sequence}`，如 `agent-task-001-extend-1`，确保同一任务的同一操作可安全重试。

> **错误处理策略**: Agent 应解析 `error` 字段中的 traceback，提取关键信息（错误类型、行号、变量名），用于生成修复建议。

---

## 场景 7: 路径安全与容器隔离测试 (安全边界验证)

**用户画像**: 安全测试人员 / 恶意用户尝试突破沙箱边界。

**目标**: 验证 Bay 和容器的多层安全防护：
- Bay API 层对路径穿越的前置校验
- 容器隔离对 Python/Shell 代码执行的限制
- 理解哪些"攻击"是被阻止的，哪些是容器隔离范围内的预期行为

**测试文件**: `pkgs/bay/tests/integration/test_path_security.py`

### 架构行为说明

Shipyard 采用**纵深防御**策略：

```
┌──────────────────────────────────────────────────────────────────┐
│                          攻击尝试                                  │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ 第1层: Bay API 路径校验                                           │
│ - 拒绝绝对路径 (/etc/passwd)                                       │
│ - 拒绝路径穿越 (../file.txt)                                       │
│ - 返回 400 Bad Request，不到达容器                                  │
└──────────────────────────────────────────────────────────────────┘
                               │ (合法路径放行)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ 第2层: Ship API 路径解析 (resolve_path)                            │
│ - 解析符号链接                                                      │
│ - 验证最终路径在 /workspace 内                                       │
│ - 返回 403 Forbidden                                               │
└──────────────────────────────────────────────────────────────────┘
                               │ (合法路径放行)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ 第3层: Docker 容器隔离                                             │
│ - 进程 namespace 隔离                                               │
│ - 只挂载 /workspace Volume                                          │
│ - 非 root 用户执行 (shipyard)                                        │
│ - Python/Shell 代码只能访问容器内文件，无法访问宿主机                   │
└──────────────────────────────────────────────────────────────────┘
```

**关键理解**:
1. **API 层阻止**: 通过 filesystem/shell API 的路径穿越被 Bay 阻止 → 400
2. **容器内执行不受限**: Python/Shell 代码在容器内可以访问 `/etc/passwd`，但这是容器自己的文件（镜像层），不是宿主机的
3. **无法访问宿主机**: 宿主机目录未挂载到容器，Python/Shell 代码无法访问

### 行为序列

#### Part A: API 层安全验证 (Bay 阻止) - filesystem API

测试类: `TestPathSecurityE2E`

1.  **创建沙箱**: `POST /v1/sandboxes` -> 获取 `sandbox_id`

2.  **Filesystem Read - 绝对路径**: `GET /filesystem/files?path=/etc/passwd`
    - 预期: `400 Bad Request`
    - 响应: `{"error": {"code": "invalid_path", "details": {"reason": "absolute_path"}}}`

3.  **Filesystem Read - 路径穿越**: `GET /filesystem/files?path=../secret.txt`
    - 预期: `400 Bad Request`
    - 响应: `{"error": {"code": "invalid_path", "details": {"reason": "path_traversal"}}}`

4.  **Filesystem Read - 深度穿越**: `GET /filesystem/files?path=a/../../etc/passwd`
    - 预期: `400 Bad Request`
    - 响应: `{"error": {"code": "invalid_path", "details": {"reason": "path_traversal"}}}`

5.  **Filesystem Write - 绝对路径**: `PUT /filesystem/files` path: `/tmp/evil.sh`
    - 预期: `400 Bad Request`

6.  **Filesystem Write - 路径穿越**: `PUT /filesystem/files` path: `../secret.txt`
    - 预期: `400 Bad Request`

7.  **Filesystem Delete - 绝对路径**: `DELETE /filesystem/files?path=/etc/passwd`
    - 预期: `400 Bad Request`

8.  **Filesystem List - 绝对路径**: `GET /filesystem/directories?path=/etc`
    - 预期: `400 Bad Request`

9.  **Filesystem List - 路径穿越**: `GET /filesystem/directories?path=../`
    - 预期: `400 Bad Request`

10. **Filesystem Download - 绝对路径**: `GET /filesystem/download?path=/etc/passwd`
    - 预期: `400 Bad Request`

11. **Filesystem Upload - 绝对路径**: `POST /filesystem/upload` path: `/tmp/evil.txt`
    - 预期: `400 Bad Request`

12. **Filesystem Upload - 路径穿越**: `POST /filesystem/upload` path: `../../evil.txt`
    - 预期: `400 Bad Request`

#### Part A (续): API 层安全验证 - Shell API

13. **Shell cwd - 绝对路径**: `POST /shell/exec` body: `{"command": "ls", "cwd": "/etc"}`
    - 预期: `400 Bad Request`
    - 响应: `{"error": {"code": "invalid_path", "details": {"field": "cwd"}}}`

14. **Shell cwd - 路径穿越**: `POST /shell/exec` body: `{"command": "ls", "cwd": "../"}`
    - 预期: `400 Bad Request`
    - 响应: `{"error": {"code": "invalid_path", "details": {"reason": "path_traversal"}}}`

#### Part B: 容器隔离验证 (Python/Shell 代码在容器内执行)

测试类: `TestContainerIsolationE2E`

15. **Python 读取容器内 /etc/passwd**: `POST /python/exec`
    - code: `print(open('/etc/passwd').read()[:200])`
    - 预期: `success=true`
    - output 包含 `root:` (passwd 文件格式)
    - **注意**: 这是容器自己的 passwd 文件！

16. **验证 shipyard 用户存在于容器**: `POST /python/exec`
    - code: `print('shipyard' in open('/etc/passwd').read())`
    - 预期: `success=true`, output: `True`
    - **证明这是 Ship 容器的文件，而非宿主机**

17. **Shell 验证当前用户**: `POST /shell/exec`
    - body: `{"command": "whoami"}`
    - 预期: `success=true`, output: `shipyard`
    - **验证以非 root 用户执行**

18. **Shell 读取容器 /etc/passwd**: `POST /shell/exec`
    - body: `{"command": "cat /etc/passwd | grep shipyard"}`
    - 预期: `success=true`
    - output 包含 `shipyard:x:1000:1000::/workspace:/bin/bash`

19. **Python 尝试访问宿主机路径**: `POST /python/exec`
    - code: `import os; print(os.path.exists('/home'))`
    - 预期: `success=true`, output 可能为 `True`（容器有 /home）或 `False`
    - 关键是：即使存在，也是容器的 /home，不是宿主机的

20. **Python 验证 /workspace 是工作目录**: `POST /python/exec`
    - code: `import os; print(os.getcwd())`
    - 预期: `success=true`, output: `/workspace`

21. **Shell 检查挂载点**: `POST /shell/exec`
    - body: `{"command": "mount | grep /workspace || echo 'workspace mount'"}`
    - 预期: `success=true`
    - output 显示 /workspace 相关的挂载信息

#### Part C: 合法路径操作验证

测试类: `TestPathSecurityE2E` (已有) + 补充

22. **内部穿越规范化**: `GET /filesystem/files?path=subdir/../test.txt`
    - 先写入 `test.txt`
    - 预期: `200 OK` (Bay 规范化路径为 `test.txt`)
    - content: 匹配写入内容

23. **隐藏文件允许**: `PUT /filesystem/files` path: `.gitignore`
    - 预期: `200 OK`

24. **Shell 默认 cwd (None)**: `POST /shell/exec` body: `{"command": "pwd"}`
    - 预期: `success=true`, output: `/workspace`

25. **Shell 相对 cwd**: `POST /shell/exec` body: `{"command": "pwd", "cwd": "subdir"}`
    - 先创建 `subdir/dummy.txt`
    - 预期: `success=true`, output 包含 `subdir`

26. **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204

### 测试要点

| # | 测试类别 | 测试点 | 预期行为 | 防护层 |
|---|----------|--------|----------|--------|
| 1 | Part A | filesystem read 绝对路径 | 返回 400 | Bay |
| 2 | Part A | filesystem read 路径穿越 | 返回 400 | Bay |
| 3 | Part A | filesystem read 深度穿越 | 返回 400 | Bay |
| 4 | Part A | filesystem write 绝对路径 | 返回 400 | Bay |
| 5 | Part A | filesystem write 路径穿越 | 返回 400 | Bay |
| 6 | Part A | filesystem delete 绝对路径 | 返回 400 | Bay |
| 7 | Part A | filesystem list 绝对路径 | 返回 400 | Bay |
| 8 | Part A | filesystem list 路径穿越 | 返回 400 | Bay |
| 9 | Part A | filesystem download 绝对路径 | 返回 400 | Bay |
| 10 | Part A | filesystem upload 绝对路径 | 返回 400 | Bay |
| 11 | Part A | filesystem upload 路径穿越 | 返回 400 | Bay |
| 12 | Part A | shell cwd 绝对路径 | 返回 400 | Bay |
| 13 | Part A | shell cwd 路径穿越 | 返回 400 | Bay |
| 14 | Part B | Python 可读容器 /etc/passwd | success=true | 容器隔离 |
| 15 | Part B | 容器有 shipyard 用户 | output 包含 shipyard | 容器隔离 |
| 16 | Part B | Shell 以 shipyard 用户执行 | whoami = shipyard | 容器隔离 |
| 17 | Part B | 工作目录为 /workspace | getcwd = /workspace | 容器隔离 |
| 18 | Part C | 内部穿越规范化 | 返回 200 | Bay 规范化 |
| 19 | Part C | 隐藏文件允许 | 返回 200 | Bay |
| 20 | Part C | Shell cwd=None 默认工作目录 | pwd = /workspace | Ship |
| 21 | Part C | Shell cwd=相对路径 | pwd = /workspace/subdir | Ship |

### 安全设计说明

> **为什么 Python/Shell 可以读取 `/etc/passwd`？**
>
> Python/Shell 代码在容器内执行，可以访问容器内的所有文件。但这些文件来自容器镜像（只读层），不包含敏感的宿主机信息。这是**设计预期的行为**，因为：
>
> 1. **沙箱目的是隔离，而非代码审计** - 用户可以在沙箱内运行任意代码
> 2. **容器文件不敏感** - `/etc/passwd` 只有容器用户信息（如 shipyard），不含宿主机用户
> 3. **真正的安全边界是容器** - Python/Shell 代码无法逃逸容器访问宿主机
>
> 如果需要进一步限制代码能力，可以：
> - 使用 seccomp/AppArmor 限制系统调用
> - 使用 `--read-only` 容器选项
> - 创建更受限的 Profile

---

## 场景 8: Shell 驱动的 DevOps 自动化 (CI/CD 风格)

**用户画像**: DevOps 工程师 / 自动化脚本，主要通过 shell 命令完成构建、测试、部署任务。

**目标**: 验证以 shell 为主的工作流：
- 基本 shell 命令执行与输出捕获
- 使用容器内预装工具（git, node, curl 等）
- 工作目录切换（cwd 参数）
- 多步骤构建流程
- 退出码处理与错误检测

### Ship 容器预装工具

根据 [Dockerfile](../../../pkgs/ship/Dockerfile)，Ship 容器预装了以下工具：

| 类别 | 工具 | 用途 |
|------|------|------|
| 语言运行时 | `python3`, `pip` | Python 开发 |
| 语言运行时 | `node`, `npm`, `pnpm` | Node.js 开发 |
| 版本控制 | `git` | 代码管理 |
| 网络工具 | `curl` | HTTP 请求/下载 |
| 部署工具 | `vercel` | Vercel 部署 |
| 编辑器 | `vim`, `nano` | 文件编辑 |
| 监控工具 | `htop`, `ps`, `top` | 进程监控 |
| 系统工具 | `sudo` | 权限管理 |
| 文件工具 | `tar`, `gzip`, `find`, `wc` | 文件操作 |
| 文本处理 | `grep`, `sed`, `awk` | 文本处理 |

### 架构行为说明

1. **shell 执行用户**: 所有 shell 命令以 `shipyard` 用户身份执行（可通过 sudo 提权）
2. **工作目录默认值**: 未指定 cwd 时默认为 `/workspace`
3. **相对 cwd**: cwd 可以是相对于 `/workspace` 的路径，Bay 会校验安全性
4. **输出捕获**: `stdout` 和 `stderr` 分别捕获，`success` 基于退出码判断
5. **命令 shell**: 命令通过 `bash -lc` 执行，支持管道、重定向、环境变量展开

### 行为序列

#### Part A: 基础 Shell 功能

1.  **创建沙箱**: `POST /v1/sandboxes` -> 获取 `sandbox_id`

2.  **基本命令执行**: `POST /shell/exec`
    - body: `{"command": "echo 'Hello from shell'"}`
    - 预期: `success=true`, `output`: `Hello from shell`

3.  **查看工作目录**: `POST /shell/exec`
    - body: `{"command": "pwd"}`
    - 预期: `success=true`, `output`: `/workspace`

4.  **查看当前用户**: `POST /shell/exec`
    - body: `{"command": "whoami"}`
    - 预期: `success=true`, `output`: `shipyard`

5.  **环境变量展开**: `POST /shell/exec`
    - body: `{"command": "echo $HOME"}`
    - 预期: `success=true`, `output`: `/workspace`

#### Part B: 验证预装工具

6.  **Python 版本**: `POST /shell/exec`
    - body: `{"command": "python3 --version"}`
    - 预期: `success=true`, output 包含 `Python 3.13`

7.  **Node.js 版本**: `POST /shell/exec`
    - body: `{"command": "node --version"}`
    - 预期: `success=true`, output 包含 `v20` 或更高版本

8.  **npm/pnpm 可用**: `POST /shell/exec`
    - body: `{"command": "npm --version && pnpm --version"}`
    - 预期: `success=true`

9.  **git 可用**: `POST /shell/exec`
    - body: `{"command": "git --version"}`
    - 预期: `success=true`, output 包含 `git version`

10. **curl 可用**: `POST /shell/exec`
    - body: `{"command": "curl --version | head -1"}`
    - 预期: `success=true`, output 包含 `curl`

#### Part C: 模拟 Node.js 项目构建

11. **创建 package.json**: `PUT /filesystem/files`
    - path: `myapp/package.json`
    - content:
      ```json
      {
        "name": "myapp",
        "version": "1.0.0",
        "scripts": {
          "build": "echo 'Building...' && node -e \"console.log('Build complete!')\"",
          "test": "echo 'Running tests...' && exit 0"
        }
      }
      ```

12. **创建主文件**: `PUT /filesystem/files`
    - path: `myapp/index.js`
    - content:
      ```javascript
      console.log('Hello from Node.js!');
      console.log('Node version:', process.version);
      ```

13. **在项目目录执行 npm 初始化**: `POST /shell/exec`
    - body: `{"command": "npm run build", "cwd": "myapp"}`
    - 预期: `success=true`
    - output 包含:
      - `Building...`
      - `Build complete!`

14. **运行 Node.js 应用**: `POST /shell/exec`
    - body: `{"command": "node index.js", "cwd": "myapp"}`
    - 预期: `success=true`
    - output 包含:
      - `Hello from Node.js!`
      - `Node version: v`

15. **运行测试**: `POST /shell/exec`
    - body: `{"command": "npm test", "cwd": "myapp"}`
    - 预期: `success=true`
    - output 包含 `Running tests...`

#### Part D: Git 工作流

16. **初始化 git 仓库**: `POST /shell/exec`
    - body: `{"command": "git init myrepo"}`
    - 预期: `success=true`
    - output 包含 `Initialized empty Git repository`

17. **配置 git 用户**: `POST /shell/exec`
    - body: `{"command": "git config user.email 'test@example.com' && git config user.name 'Test User'", "cwd": "myrepo"}`
    - 预期: `success=true`

18. **创建文件并提交**: `POST /shell/exec`
    - body: `{"command": "echo 'Hello Git' > README.md && git add . && git commit -m 'Initial commit'", "cwd": "myrepo"}`
    - 预期: `success=true`
    - output 包含 `Initial commit`

19. **查看 git 日志**: `POST /shell/exec`
    - body: `{"command": "git log --oneline", "cwd": "myrepo"}`
    - 预期: `success=true`
    - output 包含 `Initial commit`

#### Part E: 管道与文本处理

20. **管道操作**: `POST /shell/exec`
    - body: `{"command": "echo -e 'line1\\nline2\\nline3' | grep line2"}`
    - 预期: `success=true`, `output`: `line2`

21. **使用 awk**: `POST /shell/exec`
    - body: `{"command": "echo 'hello world' | awk '{print $2}'"}`
    - 预期: `success=true`, `output`: `world`

22. **使用 sed**: `POST /shell/exec`
    - body: `{"command": "echo 'hello' | sed 's/hello/goodbye/'"}`
    - 预期: `success=true`, `output`: `goodbye`

23. **文件查找**: `POST /shell/exec`
    - body: `{"command": "find . -name '*.js' | head -3"}`
    - 预期: `success=true`
    - output 包含 `./myapp/index.js`

#### Part F: 错误处理与退出码

24. **命令不存在**: `POST /shell/exec`
    - body: `{"command": "nonexistent_command_12345"}`
    - 预期: `success=false`
    - `exit_code`: 非零（通常 127）

25. **显式非零退出码**: `POST /shell/exec`
    - body: `{"command": "exit 42"}`
    - 预期: `success=false`
    - `exit_code`: `42`

26. **grep 无匹配**: `POST /shell/exec`
    - body: `{"command": "echo 'hello' | grep 'xyz'"}`
    - 预期: `success=false`
    - `exit_code`: `1`（grep 无匹配时的退出码）

#### Part G: 打包与下载

27. **打包项目**: `POST /shell/exec`
    - body: `{"command": "tar -czvf myapp.tar.gz myapp/"}`
    - 预期: `success=true`
    - output 显示打包的文件列表

28. **验证压缩包**: `POST /shell/exec`
    - body: `{"command": "tar -tzvf myapp.tar.gz | head -5"}`
    - 预期: `success=true`
    - output 列出压缩包内容

29. **下载压缩包**: `GET /filesystem/download?path=myapp.tar.gz`
    - 预期: `200 OK`
    - 返回二进制内容（非空）

30. **删除沙箱**: `DELETE /v1/sandboxes/{id}` -> 204

### 测试要点

| # | 要点 | 预期行为 |
|---|------|----------|
| 1 | 基本命令执行 | echo/pwd/whoami 正常工作 |
| 2 | 环境变量 | $HOME 正确展开为 /workspace |
| 3 | Python 可用 | python3 --version 成功 |
| 4 | Node.js 可用 | node/npm/pnpm 可用 |
| 5 | git 可用 | git init/commit 工作正常 |
| 6 | curl 可用 | curl --version 成功 |
| 7 | cwd 切换 | 相对路径 cwd 正确切换目录 |
| 8 | 管道操作 | grep/awk/sed 管道正常工作 |
| 9 | 错误检测 | 非零退出码返回 `success=false` |
| 10 | 打包下载 | tar 创建的文件可下载 |

### Shell 与 Python 选择指南

> **何时使用 shell？**
> - 系统管理任务（文件操作、包管理、进程控制）
> - 调用外部工具（git, node, npm, curl）
> - 多命令流水线（管道、重定向）
> - 快速脚本执行
> - CI/CD 构建步骤
>
> **何时使用 python/exec？**
> - 复杂数据处理和分析
> - 需要保持变量状态的交互式计算
> - 使用 Python 库的任务
> - 需要结构化输出（JSON、DataFrame）
> - 机器学习和科学计算

---

## 场景 9: 超级无敌混合工作流 (Full Capability Integration)

**用户画像**: 高级用户 / 自动化框架，需要在单个任务中组合使用所有可用能力。

**背景痛点**: 真实的复杂任务往往需要组合多种能力：Python 代码执行、Shell 命令、文件系统操作、文件上传下载、TTL 管理、幂等保护、stop/resume 等。需要验证所有能力在同一沙箱生命周期内能够正确协同工作。

**目标**: 验证所有 API 能力的完整组合：
- 沙箱创建（带 Idempotency-Key）
- Python 代码执行与变量持久化
- Shell 命令执行
- 文件系统操作（读/写/删除/列表）
- 文件上传与下载（包括二进制文件）
- TTL 续命（extend_ttl）
- 停止与恢复（stop/resume）
- 容器隔离验证
- 最终清理删除

### 能力覆盖矩阵

| 能力分类 | 具体能力 | 验证步骤 |
|----------|----------|----------|
| **沙箱管理** | 创建 (POST /v1/sandboxes) | Step 1 |
| | 幂等创建 (Idempotency-Key) | Step 1, 2 |
| | 获取状态 (GET /v1/sandboxes/{id}) | Step 3 |
| | TTL 续命 (POST /extend_ttl) | Step 18 |
| | 停止 (POST /stop) | Step 20 |
| | 恢复执行 | Step 21 |
| | 删除 (DELETE) | Step 25 |
| **Python 执行** | 代码执行 (POST /python/exec) | Step 4-6, 14 |
| | 变量跨轮次共享 | Step 5-6 |
| | 错误处理与 traceback | Step 4 (可选) |
| **Shell 执行** | 命令执行 (POST /shell/exec) | Step 7-10 |
| | 管道操作 | Step 8 |
| | cwd 切换 | Step 10 |
| | 退出码检测 | Step 9 |
| **文件系统** | 写文件 (PUT /filesystem/files) | Step 11-12 |
| | 读文件 (GET /filesystem/files) | Step 13 |
| | 列目录 (GET /filesystem/directories) | Step 15 |
| | 删文件 (DELETE /filesystem/files) | Step 16 |
| **文件传输** | 上传 (POST /filesystem/upload) | Step 17 |
| | 下载 (GET /filesystem/download) | Step 19 |
| **隔离验证** | 容器用户验证 | Step 22 |
| | 工作目录验证 | Step 23 |

### 行为序列

#### Phase 1: 沙箱创建与幂等验证 (Step 1-3)

1. **创建沙箱（带 Idempotency-Key）**: `POST /v1/sandboxes`
   - body: `{ "profile": "python-default", "ttl": 600 }`
   - Header: `Idempotency-Key: mega-workflow-create-001`
   - 预期: 201, 返回 `sandbox_id`, `expires_at`

2. **幂等重试创建**: 相同 `Idempotency-Key` 再次调用 `POST /v1/sandboxes`
   - 预期: 201, 返回相同的 `sandbox_id`（回放，非重复创建）

3. **获取沙箱状态**: `GET /v1/sandboxes/{id}`
   - 预期: 200, `status: idle` (懒加载，容器未启动)

#### Phase 2: Python 代码执行 (Step 4-6)

4. **Python 执行（触发容器启动）**: `POST /python/exec`
   - code: `import sys; print(f"Python {sys.version_info.major}.{sys.version_info.minor}")`
   - 预期: `success=true`, output 包含 Python 版本
   - 容器冷启动

5. **Python 执行（定义函数）**: `POST /python/exec`
   - code: 
     ```python
     def fibonacci(n):
         if n <= 1: return n
         return fibonacci(n-1) + fibonacci(n-2)
     result = fibonacci(10)
     print(f"fib(10) = {result}")
     ```
   - 预期: `success=true`, output: `fib(10) = 55`

6. **Python 执行（复用函数，验证变量共享）**: `POST /python/exec`
   - code: `print(f"fib(15) = {fibonacci(15)}")`
   - 预期: `success=true`, output: `fib(15) = 610`

#### Phase 3: Shell 命令执行 (Step 7-10)

7. **Shell 基础命令**: `POST /shell/exec`
   - body: `{"command": "whoami && pwd"}`
   - 预期: `success=true`, output 包含 `shipyard` 和 `/workspace`

8. **Shell 管道操作**: `POST /shell/exec`
   - body: `{"command": "echo -e 'apple\\nbanana\\ncherry' | grep an"}`
   - 预期: `success=true`, output 包含 `banana`

9. **Shell 退出码检测**: `POST /shell/exec`
   - body: `{"command": "exit 42"}`
   - 预期: `success=false`, `exit_code: 42`

10. **Shell cwd 切换**: `POST /shell/exec`
    - 先创建目录：`PUT /filesystem/files` path: `workdir/marker.txt`
    - body: `{"command": "pwd && ls", "cwd": "workdir"}`
    - 预期: `success=true`, output 包含 `workdir` 和 `marker.txt`

#### Phase 4: 文件系统操作 (Step 11-16)

11. **写入代码文件**: `PUT /filesystem/files`
    - path: `src/app.py`
    - content:
      ```python
      def main():
          print("Hello from app.py!")
          return 42
      
      if __name__ == "__main__":
          main()
      ```
    - 预期: 200

12. **写入配置文件**: `PUT /filesystem/files`
    - path: `config/settings.json`
    - content: `{"debug": true, "version": "1.0.0"}`
    - 预期: 200

13. **读取文件验证**: `GET /filesystem/files?path=src/app.py`
    - 预期: 200, `content` 包含 `def main()`

14. **Python 执行文件**: `POST /python/exec`
    - code: `exec(open('src/app.py').read()); print(main())`
    - 预期: `success=true`, output 包含 `Hello from app.py!` 和 `42`

15. **列出目录结构**: `GET /filesystem/directories?path=.`
    - 预期: 200, 返回目录树包含 `src/`, `config/`, `workdir/`

16. **删除文件**: `DELETE /filesystem/files?path=workdir/marker.txt`
    - 预期: 200

#### Phase 5: 文件上传下载 (Step 17, 19)

17. **上传二进制文件**: `POST /filesystem/upload`
    - path: `data/sample.bin`
    - file: 生成的二进制内容 (如 256 字节随机数据)
    - 预期: 200

18. **TTL 续命**: `POST /v1/sandboxes/{id}/extend_ttl`
    - body: `{ "extend_by": 300 }`
    - Header: `Idempotency-Key: mega-workflow-extend-001`
    - 预期: 200, `expires_at` 更新

18.1 **TTL 续命幂等回放（模拟网络重试）**: 再次调用 `POST /v1/sandboxes/{id}/extend_ttl`
    - 相同 body + 相同 `Idempotency-Key: mega-workflow-extend-001`
    - 预期: 200，**响应体完全一致**（`expires_at` 不再变化）

19. **下载二进制文件验证**: `GET /filesystem/download?path=data/sample.bin`
    - 预期: 200, 二进制内容与上传一致

19.1 **Shell 打包（横跳到 DevOps 能力）**: `POST /shell/exec`
    - body: `{"command": "tar -czvf data.tar.gz data/"}`
    - 预期: `success=true`，output 列出打包文件

19.2 **下载压缩包（二进制）**: `GET /filesystem/download?path=data.tar.gz`
    - 预期: 200, 返回非空二进制内容（gzip magic bytes: `1f 8b`）

#### Phase 6: 启停横跳与自动唤醒 (Stop/Resume Chaos) (Step 20-21)

20. **停止沙箱**: `POST /v1/sandboxes/{id}/stop`
    - 预期: 200
    - 容器停止，Kernel 终止，Volume 保留

21. **自动唤醒验证 (Auto-Resume)**: 直接调用 `POST /python/exec` (不显式 Start/Resume)
    - 行为: `stop` 状态下的执行请求应自动触发 `ensure_running`，无需手动 resume
    - code:
      ```python
      # 变量应该丢失 (新 Session)
      try:
          print(fibonacci)
      except NameError:
          print("variable_lost_as_expected")
      
      # 文件应该保留 (同一 Volume)
      import os
      print(f"file_exists={os.path.exists('src/app.py')}")
      ```
    - 预期: `success=true`, output 包含 `variable_lost_as_expected` 和 `file_exists=True`

21.1 **混合双打：唤醒后立刻用 Shell 验证**: `POST /shell/exec`
    - body: `{"command": "ls -la && test -f src/app.py && echo 'app_py_ok'"}`
    - 预期: `success=true`，output 包含 `app_py_ok`

21.2 **再次停止 (为后续测试做准备)**: `POST /v1/sandboxes/{id}/stop`
    - 预期: 200

21.3 **安全拦截有效性 (Stop 状态)**: `GET /filesystem/files?path=/etc/passwd`
    - 行为: 在容器未运行时尝试非法路径
    - 预期: `400 Bad Request` (Bay 层静态校验应在启动容器前拦截，**不应唤醒容器**)

21.4 **文件系统自动唤醒**: `GET /filesystem/directories?path=.`
    - 行为: 文件系统读操作也应自动唤醒容器
    - 预期: 200, 返回目录结构 (容器被再次拉起)

21.5 **重复停止 (幂等性)**: 连续调用两次 `POST /v1/sandboxes/{id}/stop`
    - 预期: 两次都返回 200 (第二次无副作用)

21.6 **重建 Python 运行态并继续工作**: `POST /python/exec`
    - code:
      ```python
      def fibonacci(n):
          if n <= 1: return n
          return fibonacci(n-1) + fibonacci(n-2)
      print(f"fib(12) = {fibonacci(12)}")
      ```
    - 预期: `success=true`, output: `fib(12) = 144`

#### Phase 7: 容器隔离验证 (Step 22-24)

22. **验证用户隔离**: `POST /shell/exec`
    - body: `{"command": "id"}`
    - 预期: output 包含 `uid=1000(shipyard)`，不包含 `uid=0(root)`

23. **验证工作目录**: `POST /python/exec`
    - code: `import os; print(os.getcwd())`
    - 预期: output: `/workspace`

24. **验证容器文件系统**: `POST /python/exec`
    - code: `print('shipyard' in open('/etc/passwd').read())`
    - 预期: `success=true`, output: `True`
    - (证明读取的是容器的 passwd，不是宿主机的)

#### Phase 8: 最终清理 (Step 25)

25. **删除沙箱**: `DELETE /v1/sandboxes/{id}`
    - 预期: 204
    - 容器和 Volume 完全删除
    - 后续 `GET /v1/sandboxes/{id}` 返回 404

### 测试要点总结

| # | 能力 | 验证点 | 预期行为 |
|---|------|--------|----------|
| 1 | 幂等创建 | 相同 Key 重试 | 返回相同 sandbox_id |
| 2 | 懒加载 | 创建后状态 | idle，容器未启动 |
| 3 | 冷启动 | 首次执行 | 触发容器启动 |
| 4 | Python 变量共享 | 跨轮次 | 函数在后续轮次可用 |
| 5 | Shell 执行 | 基础命令 | 正确输出 |
| 6 | Shell 管道 | grep 管道 | 正确过滤 |
| 7 | Shell 退出码 | 非零退出 | success=false |
| 8 | Shell cwd | 相对路径切换 | pwd 显示正确目录 |
| 9 | 文件写入 | 嵌套目录 | 自动创建父目录 |
| 10 | 文件读取 | 读取内容 | 返回正确内容 |
| 11 | 目录列表 | 递归列表 | 返回完整结构 |
| 12 | 文件删除 | 删除文件 | 文件被删除 |
| 13 | 文件上传 | 二进制上传 | 正确存储 |
| 14 | 文件下载 | 二进制下载 | 内容一致 |
| 15 | TTL 续命 | extend_ttl | expires_at 更新 |
| 16 | 停止/恢复 | stop 后执行 | 变量丢失，文件保留 |
| 17 | 容器隔离 | 用户身份 | shipyard 用户 |
| 18 | 容器隔离 | 文件系统 | 容器自己的 /etc/passwd |
| 19 | 删除 | 完全清理 | 容器和 Volume 删除 |

### 测试文件

`pkgs/bay/tests/integration/test_mega_workflow.py`

### 预估执行时间

- 冷启动: 2-5 秒
- 全流程: 30-60 秒（取决于网络和 Docker 性能）

---

## 下一步

- [ ] 确认场景优先级
- [ ] 将场景转化为 `test_e2e_api.py` 中的测试用例
- [ ] 运行测试并修复发现的问题

---

## 场景 10: GC 混沌长工作流（资源回收 + 可恢复性验证）

**用户画像**: 需要长时间、多阶段执行的用户/自动化系统；期间可能出现 Bay 重启、网络抖动、TTL/Idle 边界、以及“残留资源”问题。

**目标**: 在一个“长且复杂”的真实工作流中，同时覆盖 4 个 GC 任务的关键语义与边界：

- `IdleSessionGC`：空闲回收 compute（destroy sessions），且后续 `ensure_running` 可透明重建
- `ExpiredSandboxGC`：TTL 到期后 sandbox 应被回收（不可复活），并级联清理 managed workspace
- `OrphanWorkspaceGC`：异常情况下的 managed workspace 残留兜底清理（volume + DB）
- `OrphanContainerGC`（strict）：只删除“可信且 DB 无 session”的孤儿容器（防误删）

> 备注：此场景属于 Phase 1.5 GC，但建议补充到 Phase 1 的 workflow scenarios 里作为“长期回归/混沌测试”场景（对外行为仍是 API 组合）。

### 架构行为说明（与 GC 的耦合点）

1. **Idle 维度与 TTL 维度分离**
   - `keepalive` 只延长 `idle_expires_at`，不影响 `expires_at`
   - `extend_ttl` 只延长 `expires_at`，不影响 `idle_expires_at`

2. **IdleSessionGC 回收的可恢复性**
   - IdleSessionGC 会 destroy sandbox 下所有 session（DB 记录被硬删 + runtime 被删）
   - sandbox 被置为 `current_session_id=null`、`idle_expires_at=null`
   - 用户后续任何 `python/exec`/`shell/exec` 会触发 `ensure_running()` 创建新 session → 透明恢复

3. **ExpiredSandboxGC 的不可逆性**
   - 一旦 `expires_at < now`，GC 会走 delete 语义：session destroy + sandbox 软删除 + managed workspace 级联删除
   - 此时用户再调用 `extend_ttl` 应返回 `409 sandbox_expired`（不可复活）

4. **OrphanContainerGC 的 strict 防误删门槛**
   - 只有满足 name 前缀 + 必要 labels + `bay.managed=true` + `bay.instance_id == gc.instance_id` 的容器才会进入 orphan 判定
   - orphan 判定以 DB 为准：`labels["bay.session_id"]` 在 DB 中不存在才会删

5. **OrphanWorkspaceGC 的兜底语义**
   - managed workspace 发生“部分失败/级联未完成”时，依赖该任务兜底清理

### 行为序列（建议实现为 1 条“长测试”，分 Phase 断言）

> 推荐在 E2E 环境将 `gc.interval_seconds` 调小（例如 1s），并为 strict 模式配置固定 `gc.instance_id`，详见：[`pkgs/bay/tests/scripts/docker-host/config.yaml`](../../pkgs/bay/tests/scripts/docker-host/config.yaml)

#### Phase A：建立真实工作负载（触发 session/container）

1. 创建 sandbox（带 TTL，例如 120s）：`POST /v1/sandboxes`
2. 执行 `python/exec` 写入一些文件（例如生成 `data/result.json`），确保容器与 workspace 都已创建
3. 记录 `sandbox_id`、`workspace_id`，并通过 `GET /v1/sandboxes/{id}` 确认 `status` 从 `idle` 进入 `ready/starting`

**断言**：
- sandbox 可执行、workspace 中文件存在

#### Phase B：IdleSessionGC 回收（可恢复性）

4. 调用 `POST /v1/sandboxes/{id}/keepalive`（可选：证明 idle_expires_at 会变化）
5. “人为制造 idle 超时”：将 `idle_expires_at` 强制改到过去（测试实现可直接 update E2E sqlite，如同 `test_gc_e2e.py` 的做法）
6. 等待 GC 周期运行
7. 再次 `GET /v1/sandboxes/{id}`

**断言**：
- `status == idle` 且 `idle_expires_at == null`（说明 compute 已被回收）
- 再次调用 `python/exec` 仍成功（说明可透明重建 session）
- workspace 中之前写入的文件仍存在（volume 持久化）

#### Phase C：OrphanContainerGC（strict）清理“可信 orphan”

8. 制造“可信 orphan container”（两种实现路径二选一）：
   - 路径 1（更接近真实事故）：Bay 运行中对某 sandbox 触发 session/container 后，直接从 DB 删除对应 session 记录（保留容器），等待 OrphanContainerGC 对账删除容器
   - 路径 2（更可控）：直接 `docker run -d` 创建一个带 strict labels 的容器（满足 `bay.instance_id == gc.instance_id`），但 DB 中没有该 session_id

**断言**：
- 该容器会被删除
- 同时再制造 1 个“不可信容器”（缺少 label 或 instance_id 不匹配），断言不会被删除（防误删）

#### Phase D：ExpiredSandboxGC 回收（不可复活 + workspace 清理）

9. 创建另一个 sandbox（TTL 很短，例如 1s）
10. 等待 TTL 过期
11. 等待 GC 周期运行

**断言**：
- `GET /v1/sandboxes/{id}` 返回 404（软删除后不可见）
- 对应 managed workspace volume 被删除（`docker volume inspect` 不存在）
- `POST /extend_ttl` 返回 `409 sandbox_expired`（不可复活）

#### Phase E：OrphanWorkspaceGC 兜底清理

12. 制造“孤儿 managed workspace”（建议方式）：
   - 创建 sandbox → 得到 workspace
   - 再通过 DB 操作把 `workspaces.managed_by_sandbox_id` 置空，或将对应 sandbox 标记 deleted，使 workspace 满足 OrphanWorkspaceGC 的触发条件
13. 等待 GC 周期

**断言**：
- 对应 workspace volume 被删除
- workspace DB 记录被删除

### 测试要点（为何它是“长复杂工作流”）

| 覆盖面 | 关键点 | 预期 |
|---|---|---|
| Idle 回收 | 回收 compute 不丢数据 | session/container 被回收，但 workspace 文件保留，后续 exec 可恢复 |
| TTL 回收 | TTL 到期不可复活 | sandbox 被回收，extend_ttl 409，workspace 被清理 |
| Orphan 容器兜底 | strict 防误删 | 可信 orphan 会删，不可信不会删 |
| Orphan workspace 兜底 | volume/DB 一致性 | orphan workspace 最终被清理 |

### 建议落地到测试代码的位置

- 作为单独的 E2E 测试模块：`pkgs/bay/tests/integration/test_gc_workflow_scenario.py`
- 或者把它拆成 2~3 条 E2E 测试（更稳）：
  - “IdleSessionGC 可恢复”
  - “ExpiredSandboxGC 不可复活 + workspace 清理”
  - “OrphanContainerGC strict 防误删”

---

