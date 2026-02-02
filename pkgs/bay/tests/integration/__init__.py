"""Integration/E2E tests for Bay.

目录结构（重构后）：
- `core/`：核心 API 行为（auth、sandbox lifecycle、idempotency、并发、extend_ttl）
- `filesystem/`：文件系统与传输
- `security/`：路径安全、capability/profile enforcement
- `shell/`：shell 执行与工具链
- `isolation/`：容器隔离
- `workflows/`：高层场景测试（默认串行/独占）
- `gc/`：GC 测试（必须串行/独占）

执行策略：
- 默认并行 + loadgroup：依赖 [`SERIAL_GROUPS`](pkgs/bay/tests/integration/conftest.py:62)
  在 collection 阶段统一打 `serial` / `xdist_group`。
- 建议“两阶段一口气跑完”：

    pytest pkgs/bay/tests/integration -n auto --dist loadgroup -m "not serial"
    pytest pkgs/bay/tests/integration -n 1 -m "serial"

入口说明：
- 人类入口/运行器：
  [`pkgs/bay/tests/integration/test_e2e_api.py`](pkgs/bay/tests/integration/test_e2e_api.py:1)
- 运行时脚本：
  - docker-host：
    [`pkgs/bay/tests/scripts/docker-host/run.sh`](pkgs/bay/tests/scripts/docker-host/run.sh:1)
  - docker-network：
    [`pkgs/bay/tests/scripts/docker-network/run.sh`](pkgs/bay/tests/scripts/docker-network/run.sh:1)
"""
