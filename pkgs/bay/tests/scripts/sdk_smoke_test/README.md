# SDK Smoke Tests

对 `shipyard-neo-sdk` 的端到端综合测试，依赖运行中的 Bay dev server。

## 测试文件

| 文件 | 说明 |
|:--|:--|
| `smoke_test.py` | 基础功能冒烟测试 |
| `mega_workflow_test.py` | 完整 8 阶段 Mega Workflow 集成测试 |

## 测试覆盖

### smoke_test.py

| 模块 | 测试内容 |
|:--|:--|
| **BayClient** | `create_sandbox`, `get_sandbox`, `list_sandboxes` (分页/过滤) |
| **Sandbox** | `stop`, `delete`, `extend_ttl`, `keepalive`, `refresh` |
| **PythonCapability** | `exec` (print, expression, multi-line, error, variable persistence) |
| **ShellCapability** | `exec` (simple, pipe, cwd) |
| **FilesystemCapability** | `read_file`, `write_file`, `list_dir`, `delete`, `upload`, `download` |
| **CargoManager** | `create`, `get`, `list`, `delete`, cargo 持久化验证 |
| **Error handling** | `NotFoundError` |
| **Idempotency** | `create_sandbox` 幂等键 |

### mega_workflow_test.py

| Phase | 测试内容 |
|:--|:--|
| **Phase 1** | 沙箱创建与幂等验证 |
| **Phase 2** | Python 代码执行与变量持久化 |
| **Phase 3** | Shell 命令执行 (whoami, pipe, exit code, cwd) |
| **Phase 4** | 文件系统操作 (write, read, list, delete) |
| **Phase 5** | 文件上传下载 + TTL 续命 (binary, tar.gz, idempotent extend) |
| **Phase 6** | 启停横跳与自动唤醒 (stop/resume chaos, security validation) |
| **Phase 7** | 容器隔离验证 (user, workdir, filesystem) |
| **Phase 8** | 最终清理删除 |

## 前置条件

1. **启动 Bay dev server**

   ```bash
   cd pkgs/bay
   ./tests/scripts/dev_server/start.sh
   ```

   这会：
   - 构建 ship:latest 镜像
   - 在 127.0.0.1:8002 启动 Bay 服务（允许匿名访问）

2. **安装 SDK**

   ```bash
   cd shipyard-neo-sdk
   pip install -e .
   ```

## 运行测试

```bash
# 基础冒烟测试
python pkgs/bay/tests/scripts/sdk_smoke_test/smoke_test.py

# Mega Workflow 完整集成测试
python pkgs/bay/tests/scripts/sdk_smoke_test/mega_workflow_test.py
```

## 预期输出

```
============================================================
SHIPYARD NEO SDK SMOKE TEST
============================================================

Connecting to Bay at http://127.0.0.1:8002...

============================================================
SANDBOX LIFECYCLE TESTS
============================================================

[1] Creating sandbox...
  ✓ Created sandbox: sbx_xxxx
...

============================================================
PYTHON CAPABILITY TESTS
============================================================
...

============================================================
✅ ALL SMOKE TESTS PASSED!
============================================================
```
