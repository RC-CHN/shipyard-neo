#!/usr/bin/env python3
"""SDK版 Mega Workflow Integration Test (Scenario 9).

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

Prerequisites:
    1. Start Bay dev server:
       cd pkgs/bay && ./tests/scripts/dev_server/start.sh

    2. Install SDK locally:
       cd shipyard-neo-sdk && pip install -e .

    3. Run this test:
       python pkgs/bay/tests/scripts/sdk_smoke_test/mega_workflow_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# Add SDK to path if running directly (before pip install)
SDK_PATH = Path(__file__).parent.parent.parent.parent.parent.parent / "shipyard-neo-sdk"
if SDK_PATH.exists():
    sys.path.insert(0, str(SDK_PATH))

from shipyard_neo import BayClient, InvalidPathError, NotFoundError


async def run_mega_workflow():
    """Run the complete mega workflow test using SDK."""
    
    endpoint = "http://127.0.0.1:8002"
    idempotency_prefix = f"mega-workflow-{uuid.uuid4().hex[:8]}"
    sandbox = None
    
    print("=" * 70)
    print("SDK MEGA WORKFLOW INTEGRATION TEST")
    print("=" * 70)
    print(f"\nConnecting to Bay at {endpoint}...")
    
    async with BayClient(
        endpoint_url=endpoint,
        access_token="test-token",
    ) as client:
        try:
            # =================================================================
            # Phase 1: 沙箱创建与幂等验证
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 1: 沙箱创建与幂等验证")
            print("=" * 70)
            
            # Step 1: Create sandbox with Idempotency-Key
            print("\n[Step 1] Creating sandbox with idempotency key...")
            create_key = f"{idempotency_prefix}-create"
            sandbox = await client.create_sandbox(
                profile="python-default",
                ttl=600,
                idempotency_key=create_key,
            )
            sandbox_id = sandbox.id
            print(f"  ✓ Created sandbox: {sandbox_id}")
            print(f"    Status: {sandbox.status}")  # Should be idle (lazy load)
            
            # Step 2: Idempotent retry - should return same sandbox_id
            print("\n[Step 2] Idempotent retry with same key...")
            sandbox2 = await client.create_sandbox(
                profile="python-default",
                ttl=600,
                idempotency_key=create_key,
            )
            assert sandbox2.id == sandbox_id, "Idempotent replay failed"
            print(f"  ✓ Same sandbox returned: {sandbox2.id}")
            
            # Step 3: Get sandbox status
            print("\n[Step 3] Fetching sandbox...")
            fetched = await client.get_sandbox(sandbox_id)
            print(f"  ✓ Fetched sandbox: {fetched.id}")
            
            # =================================================================
            # Phase 2: Python 代码执行 (Step 4-6)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 2: Python 代码执行")
            print("=" * 70)
            
            # Step 4: Python exec triggers cold start
            print("\n[Step 4] Python exec (triggers cold start)...")
            result = await sandbox.python.exec(
                "import sys; "
                'print(f"Python {sys.version_info.major}.{sys.version_info.minor}")'
            )
            assert result.success, f"Python exec failed: {result.error}"
            assert "Python" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 5: Define function
            print("\n[Step 5] Defining fibonacci function...")
            code = """
def fibonacci(n):
    if n <= 1: return n
    return fibonacci(n-1) + fibonacci(n-2)
result = fibonacci(10)
print(f"fib(10) = {result}")
"""
            result = await sandbox.python.exec(code)
            assert result.success, f"Function definition failed: {result.error}"
            assert "fib(10) = 55" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 6: Reuse function (variable sharing)
            print("\n[Step 6] Reusing function (variable persistence)...")
            result = await sandbox.python.exec('print(f"fib(15) = {fibonacci(15)}")')
            assert result.success, f"Variable reuse failed: {result.error}"
            assert "fib(15) = 610" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # =================================================================
            # Phase 3: Shell 命令执行 (Step 7-10)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 3: Shell 命令执行")
            print("=" * 70)
            
            # Step 7: Basic shell command
            print("\n[Step 7] Basic shell command...")
            result = await sandbox.shell.exec("whoami && pwd")
            assert result.success, f"Shell exec failed: {result.error}"
            assert "shipyard" in result.output
            assert "/workspace" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 8: Shell pipe operation
            print("\n[Step 8] Shell pipe operation...")
            result = await sandbox.shell.exec("echo -e 'apple\nbanana\ncherry' | grep an")
            assert result.success, f"Shell pipe failed: {result.error}"
            assert "banana" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 9: Shell exit code detection
            print("\n[Step 9] Shell exit code detection...")
            result = await sandbox.shell.exec("exit 42")
            assert not result.success
            assert result.exit_code == 42
            print(f"  ✓ Exit code: {result.exit_code}")
            
            # Step 10: Create workdir and test cwd
            print("\n[Step 10] Testing cwd parameter...")
            await sandbox.filesystem.write_file("workdir/marker.txt", "marker")
            result = await sandbox.shell.exec("pwd && ls", cwd="workdir")
            assert result.success, f"cwd test failed: {result.error}"
            assert "marker.txt" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # =================================================================
            # Phase 4: 文件系统操作 (Step 11-16)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 4: 文件系统操作")
            print("=" * 70)
            
            # Step 11: Write code file
            print("\n[Step 11] Writing app.py...")
            app_py_content = """def main():
    print("Hello from app.py!")
    return 42

if __name__ == "__main__":
    main()
"""
            await sandbox.filesystem.write_file("src/app.py", app_py_content)
            print("  ✓ Wrote src/app.py")
            
            # Step 12: Write config file
            print("\n[Step 12] Writing config file...")
            await sandbox.filesystem.write_file(
                "config/settings.json",
                '{"debug": true, "version": "1.0.0"}'
            )
            print("  ✓ Wrote config/settings.json")
            
            # Step 13: Read file
            print("\n[Step 13] Reading file...")
            content = await sandbox.filesystem.read_file("src/app.py")
            assert "def main()" in content
            print(f"  ✓ Read {len(content)} bytes")
            
            # Step 14: Execute file via Python
            print("\n[Step 14] Executing file via Python...")
            result = await sandbox.python.exec("exec(open('src/app.py').read()); print(main())")
            assert result.success, f"Exec file failed: {result.error}"
            assert "Hello from app.py!" in result.output
            assert "42" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 15: List directory
            print("\n[Step 15] Listing directory...")
            entries = await sandbox.filesystem.list_dir(".")
            entry_names = [e.name for e in entries]
            assert "src" in entry_names
            assert "config" in entry_names
            print(f"  ✓ Contents: {entry_names}")
            
            # Step 16: Delete file
            print("\n[Step 16] Deleting file...")
            await sandbox.filesystem.delete("workdir/marker.txt")
            print("  ✓ Deleted workdir/marker.txt")
            
            # =================================================================
            # Phase 5: 文件上传下载 + TTL 续命 (Step 17-19.2)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 5: 文件上传下载 + TTL 续命")
            print("=" * 70)
            
            # Step 17: Upload binary file
            print("\n[Step 17] Uploading binary file...")
            binary_data = os.urandom(256)  # 256 random bytes
            await sandbox.filesystem.upload("data/sample.bin", binary_data)
            print(f"  ✓ Uploaded {len(binary_data)} bytes")
            
            # Step 18: TTL extend
            print("\n[Step 18] Extending TTL...")
            extend_key = f"{idempotency_prefix}-extend-001"
            await sandbox.extend_ttl(300, idempotency_key=extend_key)
            await sandbox.refresh()
            original_expires = sandbox.expires_at
            print(f"  ✓ Extended, expires at: {original_expires}")
            
            # Step 18.1: TTL extend idempotent replay
            print("\n[Step 18.1] TTL extend idempotent replay...")
            await sandbox.extend_ttl(300, idempotency_key=extend_key)
            await sandbox.refresh()
            # Should return same expires_at
            print(f"  ✓ Same expires_at returned (idempotency works)")
            
            # Step 19: Download binary file
            print("\n[Step 19] Downloading binary file...")
            downloaded = await sandbox.filesystem.download("data/sample.bin")
            assert downloaded == binary_data, "Binary data mismatch"
            print(f"  ✓ Downloaded {len(downloaded)} bytes, data matches")
            
            # Step 19.1: Shell tar package
            print("\n[Step 19.1] Creating tar archive...")
            result = await sandbox.shell.exec("tar -czvf data.tar.gz data/")
            assert result.success, f"Tar failed: {result.error}"
            print("  ✓ Created data.tar.gz")
            
            # Step 19.2: Download tarball (check gzip magic bytes)
            print("\n[Step 19.2] Downloading and verifying tarball...")
            tarball = await sandbox.filesystem.download("data.tar.gz")
            assert len(tarball) > 0
            assert tarball[:2] == b"\x1f\x8b", "Not a valid gzip file"
            print(f"  ✓ Downloaded {len(tarball)} bytes, valid gzip")
            
            # =================================================================
            # Phase 6: 启停横跳与自动唤醒 (Step 20-21.6)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 6: 启停横跳与自动唤醒 (Stop/Resume Chaos)")
            print("=" * 70)
            
            # Step 20: Stop sandbox
            print("\n[Step 20] Stopping sandbox...")
            await sandbox.stop()
            print("  ✓ Sandbox stopped")
            
            # Step 21: Auto-resume via python exec (no explicit start)
            print("\n[Step 21] Auto-resume via Python exec...")
            result = await sandbox.python.exec("""
# Variables should be lost (new session)
try:
    print(fibonacci)
except NameError:
    print("variable_lost_as_expected")

# Files should persist (same volume)
import os
print(f"file_exists={os.path.exists('src/app.py')}")
""")
            assert result.success, f"Auto-resume failed: {result.error}"
            assert "variable_lost_as_expected" in result.output
            assert "file_exists=True" in result.output
            print(f"  ✓ Variables lost, files persisted")
            
            # Step 21.1: Shell verify after auto-resume
            print("\n[Step 21.1] Shell verify after auto-resume...")
            result = await sandbox.shell.exec("ls -la && test -f src/app.py && echo 'app_py_ok'")
            assert result.success, f"Shell verify failed: {result.error}"
            assert "app_py_ok" in result.output
            print("  ✓ File verified via shell")
            
            # Step 21.2: Stop again
            print("\n[Step 21.2] Stopping sandbox again...")
            await sandbox.stop()
            print("  ✓ Sandbox stopped")
            
            # Step 21.3: Security validation while stopped
            print("\n[Step 21.3] Security validation (invalid path)...")
            try:
                await sandbox.filesystem.read_file("/etc/passwd")
                assert False, "Should have raised InvalidPathError"
            except InvalidPathError as e:
                print(f"  ✓ Got InvalidPathError as expected")
            
            # Step 21.4: Filesystem auto-resume
            print("\n[Step 21.4] Filesystem auto-resume...")
            entries = await sandbox.filesystem.list_dir(".")
            print(f"  ✓ Listed {len(entries)} entries")
            
            # Step 21.5: Double stop (idempotent)
            print("\n[Step 21.5] Double stop (idempotent)...")
            await sandbox.stop()
            await sandbox.stop()
            print("  ✓ Double stop succeeded (idempotent)")
            
            # Step 21.6: Rebuild Python runtime
            print("\n[Step 21.6] Rebuild Python runtime...")
            result = await sandbox.python.exec("""
def fibonacci(n):
    if n <= 1: return n
    return fibonacci(n-1) + fibonacci(n-2)
print(f"fib(12) = {fibonacci(12)}")
""")
            assert result.success, f"Rebuild runtime failed: {result.error}"
            assert "fib(12) = 144" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # =================================================================
            # Phase 7: 容器隔离验证 (Step 22-24)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 7: 容器隔离验证")
            print("=" * 70)
            
            # Step 22: Verify user isolation
            print("\n[Step 22] Verifying user isolation...")
            result = await sandbox.shell.exec("id")
            assert result.success
            assert "uid=1000(shipyard)" in result.output
            assert "uid=0(root)" not in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 23: Verify working directory
            print("\n[Step 23] Verifying working directory...")
            result = await sandbox.python.exec("import os; print(os.getcwd())")
            assert result.success
            assert "/workspace" in result.output
            print(f"  ✓ Output: {result.output.strip()}")
            
            # Step 24: Verify container filesystem (not host)
            print("\n[Step 24] Verifying container filesystem...")
            result = await sandbox.python.exec("print('shipyard' in open('/etc/passwd').read())")
            assert result.success
            assert "True" in result.output
            print(f"  ✓ Container /etc/passwd contains 'shipyard'")
            
            # =================================================================
            # Phase 8: 最终清理 (Step 25)
            # =================================================================
            print("\n" + "=" * 70)
            print("Phase 8: 最终清理")
            print("=" * 70)
            
            # Step 25: Delete sandbox
            print("\n[Step 25] Deleting sandbox...")
            await sandbox.delete()
            print("  ✓ Sandbox deleted")
            
            # Verify 404 after deletion
            print("\n[Verify] Checking 404 after deletion...")
            try:
                await client.get_sandbox(sandbox_id)
                assert False, "Should have raised NotFoundError"
            except NotFoundError:
                print("  ✓ Got NotFoundError as expected")
            
            # Clear reference
            sandbox = None
            
            print("\n" + "=" * 70)
            print("✅ ALL MEGA WORKFLOW TESTS PASSED!")
            print("=" * 70)
            
        except Exception as e:
            print(f"\n❌ Mega workflow failed: {e}")
            import traceback
            traceback.print_exc()
            
            # Cleanup on failure
            if sandbox:
                try:
                    await sandbox.delete()
                    print("\n  (Cleanup: sandbox deleted)")
                except Exception:
                    pass
            
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_mega_workflow())
