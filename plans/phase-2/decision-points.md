# Phase 2 待决策事项清单

> 状态：**已决策**
> 日期：2026-02-09
> 决策日期：2026-02-09

本文档列出多容器设计中的边界情况和需要拍板的具体行为。

---

## 1. 容器生命周期管理

### 1.1 部分容器启动失败

**场景**：Profile 定义了 Ship + Browser 两个容器，Ship 启动成功但 Browser 启动失败。

| 选项 | 行为 | 优点 | 缺点 |
|------|------|------|------|
| **A. 全部回滚** | 停止 Ship，Session 标记为 FAILED | 一致性好，避免部分可用 | 用户无法使用已成功的部分 |
| **B. 部分可用** | Ship 保持运行，Session 标记为 DEGRADED | 用户可继续使用 Python | 能力不完整，可能导致混淆 |
| **C. 重试策略** | Browser 自动重试 N 次，失败后回滚 | 增加成功率 | 延长启动时间 |

**需决策**：选择哪种策略？倾向 A 还是 B？

> **✅ 最终决策：A. 全部回滚**
>
> **考虑因素**：
> - 一致性优先：部分可用状态会导致用户困惑，不知道哪些功能可用
> - 简化错误处理：统一的失败状态比 DEGRADED 更容易处理
> - Phase 2 场景简单：只有 Ship + Browser，全部回滚的代价不大
>
> **实现要点**：Session 创建时任一容器启动失败，立即停止已启动的容器，Session 标记为 FAILED。

---

### 1.2 单容器崩溃

**场景**：运行中的 Session，Browser 容器 OOM 被 kill，Ship 仍在运行。

| 选项 | 行为 |
|------|------|
| **A. 自动重启** | 检测到 Browser 挂掉后自动拉起 |
| **B. 标记降级** | Session 变为 DEGRADED，browser 能力不可用，其他继续 |
| **C. 全部停止** | 检测到任一容器挂掉，停止所有容器 |

**需决策**：是否实现自动重启？还是直接标记降级？

> **✅ 最终决策：B. 标记降级**
>
> **考虑因素**：
> - 自动重启逻辑复杂：需要考虑重启次数限制、指数退避、状态恢复等
> - Phase 2 简化优先：避免引入复杂的容器编排逻辑
> - 用户可感知：DEGRADED 状态让用户明确知道 browser 能力不可用，其他能力仍可继续使用
>
> **实现要点**：检测到容器退出时，更新 Session 状态为 DEGRADED，移除该容器的能力注册，后续请求该能力返回 503 Service Unavailable。

---

### 1.3 Idle 回收粒度

**场景**：Ship 空闲超时，但 Browser 刚被使用过。

| 选项 | 行为 |
|------|------|
| **A. 整体回收** | 任一容器空闲超时 → 回收整个 Session |
| **B. 按容器回收** | 只回收空闲的 Ship，Browser 继续运行 |
| **C. 全活跃计数** | 任一容器活跃 → 整个 Session 保持活跃 |

**需决策**：选项 C 最简单，但可能资源浪费；选项 B 最省资源但复杂度高。

> **✅ 最终决策：C. 全活跃计数**
>
> **考虑因素**：
> - 简单性优先：只需维护一个 Session 级别的 last_activity 时间戳
> - 避免部分回收的复杂性：按容器回收会导致 Session 状态变为 DEGRADED，增加状态管理复杂度
> - Phase 2 容器数量少：只有 Ship + Browser，资源浪费有限
>
> **实现要点**：任一容器收到请求时，更新 Session.last_activity；GC 只检查 Session 级别的 idle 超时。

---

## 2. 能力路由

### 2.1 无 primary_for 时的冲突解决

**场景**：两个容器都声明 `filesystem` 能力，但都没有声明 `primary_for`。

```yaml
containers:
  - name: ship
    capabilities: [filesystem, python]
    # 无 primary_for
  - name: browser
    capabilities: [filesystem, browser]
    # 无 primary_for
```

| 选项 | 行为 |
|------|------|
| **A. 第一个胜出** | 按 containers 数组顺序，第一个获胜 |
| **B. 配置校验报错** | Profile 加载时报错，强制要求声明 primary_for |
| **C. 请求时随机** | 不确定路由，可能导致行为不一致 |

**需决策**：推荐 A（简单）或 B（严格）？

> **✅ 最终决策：A. 第一个胜出**
>
> **考虑因素**：
> - 简单且确定：按 Profile 中 containers 数组顺序，第一个声明该能力的容器获胜
> - 避免启动时报错：B 选项会导致 Profile 校验失败，用户体验差
> - 文档说明：在 Profile 文档中明确说明优先级规则即可
>
> **实现要点**：CapabilityRouter 构建能力映射时，遍历 containers 数组，对每个能力只记录第一个声明它的容器。

---

### 2.2 请求级指定容器

**场景**：用户希望强制将 `filesystem` 请求发到 Browser 而非默认的 Ship。

| 选项 | 行为 |
|------|------|
| **A. 不支持** | 只按 primary_for 路由，用户无法覆盖 |
| **B. 支持 target 参数** | 请求可携带 `target: browser` 强制指定 |
| **C. 分离端点** | `/browser/files/*` vs `/ship/files/*` |

**需决策**：Phase 2 是否支持请求级指定？还是留到 Phase 3？

> **✅ 最终决策：A. 不支持**
>
> **考虑因素**：
> - 工作量影响面广：需改动 ~8 个已有端点的请求 schema + SDK + MCP Server，中等偏小但表面积大
> - Phase 2 能力不重叠：Ship 独占 python/shell，Browser 独占 browser，primary_for 路由已覆盖 99% 场景
> - 如有需要可后续只在新增的 `/browser/exec` 端点上支持 target 参数，不改已有端点
>
> **实现要点**：Phase 2 不做任何改动。如后续需要，优先考虑只在新增 Browser 端点支持 target，而非全量改造。

---

### 2.3 不存在的能力处理

**场景**：请求 `gpu` 能力，但 Profile 没有任何容器声明该能力。

| 选项 | 行为 |
|------|------|
| **A. 400 Bad Request** | 返回 CapabilityNotSupportedError |
| **B. 404 Not Found** | 能力不存在 |
| **C. 降级到 primary** | 尝试路由到主容器，可能失败 |

**需决策**：推荐 A，符合语义。

> **✅ 最终决策：A. 400 Bad Request**
>
> **考虑因素**：
> - 语义清晰：请求了不支持的能力，属于客户端错误（Bad Request）
> - 避免意外行为：降级到主容器可能产生不可预测的结果
> - 便于调试：明确的错误信息帮助用户快速定位问题
>
> **实现要点**：CapabilityRouter 查找能力时，如果没有任何容器注册该能力，抛出 CapabilityNotSupportedError（HTTP 400）。

---

## 3. Browser 特定问题

### 3.1 agent-browser 命令注入

**场景**：用户传入恶意命令 `"open https://x; rm -rf /"`。

| 选项 | 行为 |
|------|------|
| **A. 信任用户** | CLI passthrough，不做校验（沙箱隔离） |
| **B. 命令白名单** | 只允许特定命令（open, click, fill 等） |
| **C. 参数转义** | 对 cmd 做 shell 转义 |

**需决策**：考虑到已有容器隔离，是否采用 A？

> **✅ 最终决策：A. 信任用户**
>
> **考虑因素**：
> - 沙箱隔离：Browser 容器本身就是隔离环境，即使执行恶意命令也不影响宿主机
> - 灵活性：白名单会限制 agent-browser 的扩展能力，新增命令需要同步更新白名单
> - 简化实现：不需要维护命令白名单或复杂的转义逻辑
>
> **实现要点**：BrowserAdapter 直接将用户命令透传给 agent-browser CLI，不做额外校验。

---

### 3.2 浏览器会话持久化

**场景**：Session 被 idle 回收后重建，Browser 状态（登录态、cookies）丢失。

| 选项 | 行为 |
|------|------|
| **A. 不保留** | 重建后是全新浏览器，用户需重新登录 |
| **B. 自动保存/恢复** | 回收前 `state save`，重建后 `state load` |
| **C. 用户自行管理** | 提供 API 让用户手动 save/load |

**需决策**：Phase 2 是否实现自动保存？还是仅提供手动接口？

> **✅ 最终决策：B 轻量版 - 自动保存到 Cargo Volume**
>
> **考虑因素**：
> - 复用 Cargo Volume：所有容器共享 `/workspace`，零额外存储成本
> - 用户体验好：同一 Sandbox 的多次 Session 自动恢复浏览器状态，无需用户干预
> - 实现简单：只需在 Browser Runtime 的启动/关闭钩子中各加一条 agent-browser 命令
> - Sandbox 删除时 Cargo Volume 一起清理，状态自然回收
>
> **实现要点**：
> - Browser 容器启动时：检查 `/workspace/.browser/state/` 是否存在，存在则 `state load`
> - Browser 容器关闭前（GC 回收时）：执行 `state save` 到 `/workspace/.browser/state/`
> - 可通过 Browser Runtime 容器的 SIGTERM handler 自动执行 state save

---

### 3.3 截图/下载文件存储位置

**场景**：用户调用 `screenshot` 不指定路径。

| 选项 | 行为 |
|------|------|
| **A. 强制指定路径** | 400 错误，要求用户提供 path |
| **B. 默认存到 /workspace** | 自动生成唯一文件名 `/workspace/screenshot_xxx.png` |
| **C. 临时目录** | 存到 /tmp，通过返回的 URL 下载 |

**需决策**：推荐 B，与 Cargo 机制一致。

> **✅ 最终决策：B. 默认存到 /workspace**
>
> **考虑因素**：
> - 与 Cargo 机制一致：文件存储在共享 Volume，对用户透明
> - 便于后续访问：用户可通过 filesystem API 直接读取截图文件
> - 自动清理：Sandbox 删除时 Cargo Volume 一起清理
>
> **实现要点**：截图默认保存到 `/workspace/screenshot_{timestamp}.png`，返回文件路径。

---

## 4. 多容器网络

### 4.1 容器间通信

**场景**：Ship 需要调用 Browser API（如 Python 代码控制浏览器）。

| 选项 | 行为 |
|------|------|
| **A. 不支持直接通信** | 必须通过 Bay API 中转 |
| **B. 共享网络** | 容器在同一 Docker network，可通过 hostname 互访 |
| **C. Sidecar 注入** | 注入 proxy sidecar 实现互访 |

**需决策**：Phase 2 是否支持容器间直连？推荐 B（简单）。

> **✅ 最终决策：B. 共享网络**
>
> **考虑因素**：
> - 简单：Docker 原生支持同一 network 内的容器互访，无需额外代理
> - 低延迟：容器间直连避免了通过 Bay API 中转的网络开销
> - 扩展性好：后续新增容器类型自动加入同一网络即可
>
> **实现要点**：每个 Session 创建独立的 Docker network，所有容器加入该网络。Session 销毁时清理网络。

---

### 4.2 容器 hostname 命名

**场景**：如果支持容器间通信，hostname 怎么命名？

| 选项 | 示例 |
|------|------|
| **A. 容器名** | `ship`, `browser` |
| **B. session_id + 容器名** | `sess_abc_ship`, `sess_abc_browser` |
| **C. sandbox_id + 容器名** | `sbx_123_ship`, `sbx_123_browser` |

**需决策**：推荐 A（简单），在同一 Session 网络内唯一即可。

> **✅ 最终决策：A. 容器名**
>
> **考虑因素**：
> - 简单直观：在同一 Session 网络内，容器名天然唯一
> - 用户友好：用户在 Ship 容器中可直接用 `http://browser:port` 访问 Browser
> - 无需复杂命名规则：避免过长的 hostname 导致的可读性问题
>
> **实现要点**：Docker 容器创建时设置 `hostname` 和 `network_aliases` 为容器 name（如 `ship`、`browser`）。

---

## 5. Session 模型

### 5.1 containers 字段格式

**场景**：Session.containers 存储运行时容器信息。

| 选项 | 格式 |
|------|------|
| **A. JSON 数组** | `[{name, container_id, endpoint, ...}]` |
| **B. JSON 对象** | `{ship: {container_id, ...}, browser: {...}}` |

**需决策**：推荐 A（与 Profile 格式一致）。

> **✅ 最终决策：A. JSON 数组**
>
> **考虑因素**：
> - 与 Profile 格式一致：Profile 中 containers 也是数组格式，保持统一
> - 保留顺序信息：数组天然保留容器定义顺序，用于冲突解决（第一个胜出）
> - 便于遍历：API 返回和内部处理都可直接迭代
>
> **实现要点**：Session.containers 字段存储为 `[{name, container_id, endpoint, status, capabilities}]`。

---

### 5.2 主容器（primary）定义

**场景**：如何确定 Session 的主容器？

| 选项 | 规则 |
|------|------|
| **A. 第一个容器** | containers[0] 自动成为 primary |
| **B. 名称匹配** | name == "primary" 或 name == "ship" |
| **C. Profile 显式声明** | Profile 中 `primary_container: ship` |

**需决策**：推荐 A + B fallback（简单且直观）。

> **✅ 最终决策：A + B fallback**
>
> **考虑因素**：
> - 简单直观：默认第一个容器为主容器，符合直觉
> - 兼容性好：如果容器名为 "ship" 或 "primary"，也自动识别为主容器
> - 向后兼容：单容器 Profile 天然兼容（只有一个容器，即为 primary）
>
> **实现要点**：优先检查 containers 中是否有 name 为 "ship" 或 "primary" 的容器；如果没有，则取 containers[0]。

---

## 6. GC 与资源管理

### 6.1 多容器 GC 原子性

**场景**：GC 检测到 Session 应该回收。

| 选项 | 行为 |
|------|------|
| **A. 原子删除** | 一次性停止所有容器，部分失败则全部回滚 |
| **B. 尽力删除** | 逐个停止，失败的跳过，记录日志 |
| **C. 标记后删除** | 先标记为 STOPPING，后台异步清理 |

**需决策**：推荐 C（与现有 GC 一致）。

> **✅ 最终决策：C. 标记后删除**
>
> **考虑因素**：
> - 与现有 GC 机制一致：当前已有 STOPPING 状态的异步清理流程
> - 避免阻塞：原子删除可能因某个容器响应慢而阻塞整个 GC 循环
> - 容错性好：标记后异步清理，即使部分容器清理失败也不影响其他 GC 任务
>
> **实现要点**：GC 检测到需回收时，先将 Session 标记为 STOPPING，然后异步逐个停止容器，最后清理网络和 Volume。

---

### 6.2 资源配额计算

**场景**：多容器 Session 的资源限制如何计算？

| 选项 | 行为 |
|------|------|
| **A. 每容器独立** | 每个容器按自己的 resources 限制 |
| **B. Session 总额** | 所有容器共享一个资源池 |

**需决策**：推荐 A（Docker/K8s 原生行为）。

> **✅ 最终决策：A. 每容器独立**
>
> **考虑因素**：
> - Docker/K8s 原生行为：容器运行时本身就是按容器隔离资源的
> - 简单：无需额外的资源池管理逻辑
> - 精确控制：每个容器的资源需求不同（Browser 需要更多内存），独立配置更合理
>
> **实现要点**：Profile 中每个容器定义各自的 resources（cpu、memory），创建容器时直接传给 Docker/K8s。

---

## 7. API 兼容性

### 7.1 现有 API 行为

**场景**：多容器 Session 调用 `POST /python/exec`。

| 选项 | 行为 |
|------|------|
| **A. 自动路由** | 路由到支持 python 能力的容器 |
| **B. 失败** | 返回 404，要求使用新 API |

**需决策**：推荐 A（向后兼容）。

> **✅ 最终决策：A. 自动路由**
>
> **考虑因素**：
> - 向后兼容：现有 SDK 和 MCP Server 无需修改即可在多容器环境下工作
> - 透明性：用户不需要关心哪个容器处理了请求，CapabilityRouter 自动完成路由
> - 符合能力路由设计：这正是 CapabilityRouter 的核心价值
>
> **实现要点**：现有 API 端点通过 CapabilityRouter 自动路由到正确的容器，无需任何改动。

---

### 7.2 /meta API 返回

**场景**：`GET /sandboxes/{id}/meta` 应该返回什么？

| 选项 | 行为 |
|------|------|
| **A. 合并所有容器能力** | `{capabilities: {python, shell, browser, ...}}` |
| **B. 按容器分组** | `{containers: [{name: ship, capabilities: [...]}, ...]}` |
| **C. 两者都返回** | 同时提供聚合视图和详细视图 |

**需决策**：推荐 C（兼容现有客户端 + 提供详情）。

> **✅ 最终决策：C. 两者都返回**
>
> **考虑因素**：
> - 兼容现有客户端：聚合视图 `capabilities` 字段保持向后兼容
> - 提供详情：新增 `containers` 字段让高级用户了解容器拓扑
> - 渐进增强：现有客户端忽略新字段，新客户端可利用详情
>
> **实现要点**：`/meta` 返回同时包含 `capabilities`（所有容器能力的合并列表）和 `containers`（按容器分组的详细信息）。

---

## 8. 决策优先级

按实现阻塞程度排序：

| 优先级 | 决策点 | 原因 |
|--------|--------|------|
| P0 | 1.1 部分容器启动失败 | 影响 Session 创建流程 |
| P0 | 2.1 无 primary_for 冲突解决 | 影响 Profile 校验逻辑 |
| P0 | 5.1 containers 字段格式 | 影响数据模型设计 |
| P1 | 4.1 容器间通信 | 影响 Docker 网络配置 |
| P1 | 3.1 命令注入 | 影响安全设计 |
| P2 | 1.2 单容器崩溃 | 可后续迭代 |
| P2 | 3.2 浏览器状态持久化 | 可后续迭代 |
| P2 | 2.2 请求级指定容器 | 可后续迭代 |

---

## 9. 建议的默认决策

如果需要快速推进，以下是建议的默认选择：

| 决策点 | 建议选项 | 理由 |
|--------|---------|------|
| 1.1 启动失败 | A 全部回滚 | 简单一致 |
| 1.2 单容器崩溃 | B 标记降级 | 简单，避免复杂重启逻辑 |
| 1.3 Idle 回收 | C 全活跃计数 | 简单 |
| 2.1 冲突解决 | A 第一个胜出 | 简单，文档说明 |
| 2.2 请求级指定 | A 不支持 | Phase 2 简化 |
| 2.3 不存在能力 | A 400 错误 | 语义清晰 |
| 3.1 命令注入 | A 信任用户 | 沙箱隔离 |
| 3.2 状态持久化 | B 轻量版（Cargo Volume 自动保存） | 零额外存储，体验好 |
| 3.3 截图路径 | B 默认 /workspace | 与 Cargo 一致 |
| 4.1 容器间通信 | B 共享网络 | 简单 |
| 4.2 hostname | A 容器名 | 简单 |
| 5.1 containers 格式 | A JSON 数组 | 与 Profile 一致 |
| 5.2 主容器定义 | A+B fallback | 简单直观 |
| 6.1 GC 原子性 | C 标记后删除 | 与现有一致 |
| 6.2 资源配额 | A 每容器独立 | 原生行为 |
| 7.1 现有 API | A 自动路由 | 向后兼容 |
| 7.2 /meta 返回 | C 两者都返回 | 兼容 + 详情 |
