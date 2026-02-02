"""E2E-19: Mega Workflow Integration Test (Scenario 9).

超级无敌混合工作流 - 验证所有 API 能力的完整组合：
- 沙箱创建（带 Idempotency-Key）
- Python 代码执行与变量持久化
- Shell 命令执行
- 文件系统操作（读/写/删除/列表）
- 文件上传与下载（包括二进制文件）
- TTL 续命（extend_ttl）
- 停止与恢复（stop/resume）及自动唤醒
- 容器隔离验证
- 最终清理删除

See: plans/phase-1/e2e-workflow-scenarios.md Scenario 9
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

# Apply skip conditions
pytestmark = e2e_skipif_marks


class TestMegaWorkflowE2E:
    """Scenario 9: 超级无敌混合工作流 (Full Capability Integration).

    Tests the complete combination of all API capabilities in a single
    sandbox lifecycle.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test - create a unique idempotency key prefix for this run."""
        self.idempotency_prefix = f"mega-workflow-{uuid.uuid4().hex[:8]}"
        self.sandbox_id = None

    def teardown_method(self, method):
        """Cleanup - ensure sandbox is deleted even if test fails."""
        if self.sandbox_id:
            with httpx.Client(timeout=30.0) as client:
                try:
                    client.delete(
                        f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}",
                        headers=AUTH_HEADERS,
                    )
                except Exception:
                    pass

    # =========================================================================
    # Phase 1: 沙箱创建与幂等验证 (Step 1-3)
    # =========================================================================

    def test_mega_workflow_complete(self):
        """Test the complete mega workflow as a single coherent test.

        This test exercises all capabilities in sequence to verify they
        work together correctly in a real-world scenario.
        """
        with httpx.Client(timeout=120.0) as client:
            # -----------------------------------------------------------------
            # Phase 1: 沙箱创建与幂等验证
            # -----------------------------------------------------------------

            # Step 1: Create sandbox with Idempotency-Key
            create_key = f"{self.idempotency_prefix}-create"
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 600},
                headers={**AUTH_HEADERS, "Idempotency-Key": create_key},
            )
            assert response.status_code == 201, f"Create failed: {response.text}"
            sandbox = response.json()
            self.sandbox_id = sandbox["id"]
            assert sandbox["status"] == "idle"  # Lazy load, container not started yet

            # Step 2: Idempotent retry - should return same sandbox_id
            response2 = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 600},
                headers={**AUTH_HEADERS, "Idempotency-Key": create_key},
            )
            assert response2.status_code == 201
            assert response2.json()["id"] == self.sandbox_id, "Idempotent replay failed"

            # Step 3: Get sandbox status
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            # Status might still be idle (lazy load)

            # -----------------------------------------------------------------
            # Phase 2: Python 代码执行 (Step 4-6)
            # -----------------------------------------------------------------

            # Step 4: Python exec triggers cold start
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={
                    "code": 'import sys; print(f"Python {sys.version_info.major}.{sys.version_info.minor}")'
                },
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200, f"Python exec failed: {response.text}"
            result = response.json()
            assert result["success"] is True
            assert "Python" in result["output"]

            # Step 5: Define function
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={
                    "code": """
def fibonacci(n):
    if n <= 1: return n
    return fibonacci(n-1) + fibonacci(n-2)
result = fibonacci(10)
print(f"fib(10) = {result}")
"""
                },
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "fib(10) = 55" in result["output"]

            # Step 6: Reuse function (variable sharing)
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={"code": 'print(f"fib(15) = {fibonacci(15)}")'},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "fib(15) = 610" in result["output"]

            # -----------------------------------------------------------------
            # Phase 3: Shell 命令执行 (Step 7-10)
            # -----------------------------------------------------------------

            # Step 7: Basic shell command
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "whoami && pwd"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "shipyard" in result["output"]
            assert "/workspace" in result["output"]

            # Step 8: Shell pipe operation
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "echo -e 'apple\\nbanana\\ncherry' | grep an"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "banana" in result["output"]

            # Step 9: Shell exit code detection
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "exit 42"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is False
            assert result["exit_code"] == 42

            # Step 10: Create workdir and test cwd
            response = client.put(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/files",
                json={"path": "workdir/marker.txt", "content": "marker"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "pwd && ls", "cwd": "workdir"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "marker.txt" in result["output"]

            # -----------------------------------------------------------------
            # Phase 4: 文件系统操作 (Step 11-16)
            # -----------------------------------------------------------------

            # Step 11: Write code file
            app_py_content = """def main():
    print("Hello from app.py!")
    return 42

if __name__ == "__main__":
    main()
"""
            response = client.put(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/files",
                json={"path": "src/app.py", "content": app_py_content},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            # Step 12: Write config file
            response = client.put(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/files",
                json={
                    "path": "config/settings.json",
                    "content": '{"debug": true, "version": "1.0.0"}',
                },
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            # Step 13: Read file
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/files",
                params={"path": "src/app.py"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            assert "def main()" in response.json()["content"]

            # Step 14: Execute file via Python
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={"code": "exec(open('src/app.py').read()); print(main())"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Hello from app.py!" in result["output"]
            assert "42" in result["output"]

            # Step 15: List directory
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/directories",
                params={"path": "."},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            entries = response.json()["entries"]
            entry_names = [e["name"] for e in entries]
            assert "src" in entry_names
            assert "config" in entry_names

            # Step 16: Delete file
            response = client.delete(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/files",
                params={"path": "workdir/marker.txt"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            # -----------------------------------------------------------------
            # Phase 5: 文件上传下载 + TTL 续命 (Step 17-19.2)
            # -----------------------------------------------------------------

            # Step 17: Upload binary file
            binary_data = os.urandom(256)  # 256 random bytes
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/upload",
                files={"file": ("sample.bin", binary_data)},
                data={"path": "data/sample.bin"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            # Step 18: TTL extend
            extend_key = f"{self.idempotency_prefix}-extend-001"
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/extend_ttl",
                json={"extend_by": 300},
                headers={**AUTH_HEADERS, "Idempotency-Key": extend_key},
            )
            assert response.status_code == 200
            original_expires = response.json()["expires_at"]

            # Step 18.1: TTL extend idempotent replay
            response2 = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/extend_ttl",
                json={"extend_by": 300},
                headers={**AUTH_HEADERS, "Idempotency-Key": extend_key},
            )
            assert response2.status_code == 200
            assert (
                response2.json()["expires_at"] == original_expires
            ), "Idempotent replay should return same expires_at"

            # Step 19: Download binary file
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/download",
                params={"path": "data/sample.bin"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            assert response.content == binary_data

            # Step 19.1: Shell tar package
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "tar -czvf data.tar.gz data/"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            assert response.json()["success"] is True

            # Step 19.2: Download tarball (check gzip magic bytes)
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/download",
                params={"path": "data.tar.gz"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            assert len(response.content) > 0
            # gzip magic bytes: 1f 8b
            assert response.content[:2] == b"\x1f\x8b", "Not a valid gzip file"

            # -----------------------------------------------------------------
            # Phase 6: 启停横跳与自动唤醒 (Stop/Resume Chaos) (Step 20-21.6)
            # -----------------------------------------------------------------

            # Step 20: Stop sandbox
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/stop",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            # Step 21: Auto-resume via python exec (no explicit start)
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={
                    "code": """
# Variables should be lost (new session)
try:
    print(fibonacci)
except NameError:
    print("variable_lost_as_expected")

# Files should persist (same volume)
import os
print(f"file_exists={os.path.exists('src/app.py')}")
"""
                },
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "variable_lost_as_expected" in result["output"]
            assert "file_exists=True" in result["output"]

            # Step 21.1: Shell verify after auto-resume
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "ls -la && test -f src/app.py && echo 'app_py_ok'"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "app_py_ok" in result["output"]

            # Step 21.2: Stop again
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/stop",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200

            # Step 21.3: Security validation while stopped
            # Bay should reject invalid path before starting container
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/files",
                params={"path": "/etc/passwd"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 400
            error = response.json()
            assert error["error"]["code"] == "invalid_path"

            # Step 21.4: Filesystem auto-resume
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/filesystem/directories",
                params={"path": "."},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            # Container should be running again now

            # Step 21.5: Double stop (idempotent)
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/stop",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/stop",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200  # Should not fail

            # Step 21.6: Rebuild Python runtime
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={
                    "code": """
def fibonacci(n):
    if n <= 1: return n
    return fibonacci(n-1) + fibonacci(n-2)
print(f"fib(12) = {fibonacci(12)}")
"""
                },
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "fib(12) = 144" in result["output"]

            # -----------------------------------------------------------------
            # Phase 7: 容器隔离验证 (Step 22-24)
            # -----------------------------------------------------------------

            # Step 22: Verify user isolation
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/shell/exec",
                json={"command": "id"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "uid=1000(shipyard)" in result["output"]
            assert "uid=0(root)" not in result["output"]

            # Step 23: Verify working directory
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={"code": "import os; print(os.getcwd())"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "/workspace" in result["output"]

            # Step 24: Verify container filesystem (not host)
            response = client.post(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}/python/exec",
                json={"code": "print('shipyard' in open('/etc/passwd').read())"},
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "True" in result["output"]

            # -----------------------------------------------------------------
            # Phase 8: 最终清理 (Step 25)
            # -----------------------------------------------------------------

            # Step 25: Delete sandbox
            response = client.delete(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 204

            # Verify 404 after deletion
            response = client.get(
                f"{BAY_BASE_URL}/v1/sandboxes/{self.sandbox_id}",
                headers=AUTH_HEADERS,
            )
            assert response.status_code == 404

            # Clear sandbox_id to prevent teardown from trying to delete again
            self.sandbox_id = None
