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

## 下一步

- [ ] 确认场景优先级
- [ ] 将场景转化为 `test_e2e_api.py` 中的测试用例
- [ ] 运行测试并修复发现的问题
