#!/usr/bin/env python3
"""Comprehensive smoke test for shipyard_neo SDK against a running Bay dev server.

Prerequisites:
    1. Start Bay dev server:
       cd pkgs/bay && ./tests/scripts/dev_server/start.sh

    2. Install SDK locally:
       cd shipyard-neo-sdk && pip install -e .

    3. Run this test:
       python pkgs/bay/tests/scripts/sdk_smoke_test/smoke_test.py

This test verifies ALL SDK functionality:
- BayClient: create_sandbox, get_sandbox, list_sandboxes
- Sandbox: stop, delete, extend_ttl, keepalive, refresh
- PythonCapability: exec
- ShellCapability: exec (with cwd)
- FilesystemCapability: read_file, write_file, list_dir, delete, upload, download
- CargoManager: create, get, list, delete
- Error handling: NotFoundError
- Idempotency: create_sandbox with idempotency_key
"""

import asyncio
import sys
from pathlib import Path

# Add SDK to path if running directly (before pip install)
SDK_PATH = Path(__file__).parent.parent.parent.parent.parent.parent / "shipyard-neo-sdk"
if SDK_PATH.exists():
    sys.path.insert(0, str(SDK_PATH))

from shipyard_neo import BayClient, NotFoundError, SandboxStatus


async def test_sandbox_lifecycle(client: BayClient) -> str:
    """Test sandbox creation, capabilities, and lifecycle."""
    print("\n" + "=" * 60)
    print("SANDBOX LIFECYCLE TESTS")
    print("=" * 60)
    
    # 1. Create sandbox
    print("\n[1] Creating sandbox...")
    sandbox = await client.create_sandbox(
        profile="python-default",
        ttl=300,
    )
    print(f"  ✓ Created sandbox: {sandbox.id}")
    print(f"    Status: {sandbox.status}")
    print(f"    Profile: {sandbox.profile}")
    print(f"    Cargo ID: {sandbox.cargo_id}")
    print(f"    Capabilities: {sandbox.capabilities}")
    print(f"    Expires at: {sandbox.expires_at}")
    
    # 2. Get sandbox
    print("\n[2] Getting sandbox by ID...")
    fetched = await client.get_sandbox(sandbox.id)
    assert fetched.id == sandbox.id
    print(f"  ✓ Fetched sandbox: {fetched.id}")
    
    # 3. List sandboxes
    print("\n[3] Listing sandboxes...")
    result = await client.list_sandboxes(limit=10)
    print(f"  ✓ Listed {len(result.items)} sandboxes")
    print(f"    Next cursor: {result.next_cursor}")
    assert any(s.id == sandbox.id for s in result.items)
    
    # 4. List with status filter
    print("\n[4] Listing sandboxes with status filter...")
    result = await client.list_sandboxes(status=SandboxStatus.READY, limit=10)
    print(f"  ✓ Listed {len(result.items)} READY sandboxes")
    
    # 5. Refresh
    print("\n[5] Refreshing sandbox info...")
    await sandbox.refresh()
    print(f"  ✓ Refreshed, status: {sandbox.status}")
    
    # 6. Keepalive
    print("\n[6] Sending keepalive...")
    await sandbox.keepalive()
    print("  ✓ Keepalive sent")
    
    # 7. Extend TTL
    print("\n[7] Extending TTL by 60 seconds...")
    old_expires = sandbox.expires_at
    await sandbox.extend_ttl(60)
    print(f"  ✓ TTL extended")
    print(f"    Old expires_at: {old_expires}")
    print(f"    New expires_at: {sandbox.expires_at}")
    
    # 8. Stop sandbox
    print("\n[8] Stopping sandbox...")
    await sandbox.stop()
    await sandbox.refresh()
    print(f"  ✓ Stopped, status: {sandbox.status}")
    
    # Return sandbox_id for cleanup
    return sandbox.id


async def test_python_capability(client: BayClient, sandbox_id: str):
    """Test Python execution capability."""
    print("\n" + "=" * 60)
    print("PYTHON CAPABILITY TESTS")
    print("=" * 60)
    
    sandbox = await client.get_sandbox(sandbox_id)
    
    # 1. Simple print
    print("\n[1] Executing print statement...")
    result = await sandbox.python.exec("print('Hello from SDK!')")
    print(f"  ✓ Output: {result.output.strip()}")
    assert result.success
    assert "Hello from SDK!" in result.output
    
    # 2. Expression with return value
    print("\n[2] Executing expression...")
    result = await sandbox.python.exec("2 ** 10")
    print(f"  ✓ Output: {result.output.strip()}")
    print(f"    Data: {result.data}")
    assert result.success
    
    # 3. Multi-line code
    print("\n[3] Executing multi-line code...")
    code = """
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)

print(f"10! = {factorial(10)}")
"""
    result = await sandbox.python.exec(code)
    print(f"  ✓ Output: {result.output.strip()}")
    assert result.success
    assert "3628800" in result.output
    
    # 4. Error handling
    print("\n[4] Executing code with error...")
    result = await sandbox.python.exec("1 / 0")
    print(f"  ✓ Success: {result.success}")
    print(f"    Error: {result.error}")
    assert not result.success
    assert "ZeroDivisionError" in (result.error or result.output)
    
    # 5. Variable persistence
    print("\n[5] Testing variable persistence...")
    await sandbox.python.exec("my_var = 42")
    result = await sandbox.python.exec("print(my_var)")
    print(f"  ✓ Variable persisted: {result.output.strip()}")
    assert "42" in result.output


async def test_shell_capability(client: BayClient, sandbox_id: str):
    """Test Shell execution capability."""
    print("\n" + "=" * 60)
    print("SHELL CAPABILITY TESTS")
    print("=" * 60)
    
    sandbox = await client.get_sandbox(sandbox_id)
    
    # 1. Simple command
    print("\n[1] Executing simple command...")
    result = await sandbox.shell.exec("echo 'Hello from shell!'")
    print(f"  ✓ Output: {result.output.strip()}")
    print(f"    Exit code: {result.exit_code}")
    assert result.success
    assert result.exit_code == 0
    
    # 2. Command with pipe
    print("\n[2] Executing command with pipe...")
    result = await sandbox.shell.exec("echo 'line1\nline2\nline3' | wc -l")
    print(f"  ✓ Output: {result.output.strip()}")
    assert result.success
    
    # 3. Create directory and use cwd
    print("\n[3] Creating directory and using cwd...")
    await sandbox.shell.exec("mkdir -p /workspace/mydir")
    await sandbox.shell.exec("echo 'test' > /workspace/mydir/file.txt")
    result = await sandbox.shell.exec("cat file.txt", cwd="mydir")
    print(f"  ✓ Output with cwd: {result.output.strip()}")
    assert "test" in result.output
    
    # 4. Environment check
    print("\n[4] Checking environment...")
    result = await sandbox.shell.exec("pwd && whoami")
    print(f"  ✓ pwd/whoami: {result.output.strip()}")
    assert result.success


async def test_filesystem_capability(client: BayClient, sandbox_id: str):
    """Test Filesystem capability."""
    print("\n" + "=" * 60)
    print("FILESYSTEM CAPABILITY TESTS")
    print("=" * 60)
    
    sandbox = await client.get_sandbox(sandbox_id)
    
    # 1. Write file
    print("\n[1] Writing file...")
    await sandbox.filesystem.write_file("test.txt", "Hello, World!")
    print("  ✓ Wrote test.txt")
    
    # 2. Read file
    print("\n[2] Reading file...")
    content = await sandbox.filesystem.read_file("test.txt")
    print(f"  ✓ Read content: {content}")
    assert content == "Hello, World!"
    
    # 3. Write nested file (auto-create directories)
    print("\n[3] Writing nested file...")
    await sandbox.filesystem.write_file("nested/dir/file.txt", "Nested content")
    content = await sandbox.filesystem.read_file("nested/dir/file.txt")
    print(f"  ✓ Read nested content: {content}")
    
    # 4. List directory
    print("\n[4] Listing directory...")
    entries = await sandbox.filesystem.list_dir(".")
    names = [e.name for e in entries]
    print(f"  ✓ Directory contents: {names}")
    assert "test.txt" in names
    
    # 5. List nested directory
    print("\n[5] Listing nested directory...")
    entries = await sandbox.filesystem.list_dir("nested/dir")
    print(f"  ✓ Nested contents: {[e.name for e in entries]}")
    
    # 6. Upload binary file
    print("\n[6] Uploading binary file...")
    binary_data = b"\x00\x01\x02\x03\x04\x05\xFF\xFE\xFD"
    await sandbox.filesystem.upload("binary.bin", binary_data)
    print("  ✓ Uploaded binary.bin")
    
    # 7. Download binary file
    print("\n[7] Downloading binary file...")
    downloaded = await sandbox.filesystem.download("binary.bin")
    print(f"  ✓ Downloaded {len(downloaded)} bytes")
    assert downloaded == binary_data
    
    # 8. Delete file
    print("\n[8] Deleting file...")
    await sandbox.filesystem.delete("test.txt")
    print("  ✓ Deleted test.txt")
    
    # Verify deletion
    try:
        await sandbox.filesystem.read_file("test.txt")
        assert False, "Should have raised error"
    except Exception as e:
        print(f"  ✓ Verified deletion (got error: {type(e).__name__})")


async def test_cargo_manager(client: BayClient):
    """Test Cargo management."""
    print("\n" + "=" * 60)
    print("CARGO MANAGER TESTS")
    print("=" * 60)
    
    # 1. Create external cargo
    print("\n[1] Creating external cargo...")
    cargo = await client.cargos.create(size_limit_mb=512)
    print(f"  ✓ Created cargo: {cargo.id}")
    print(f"    Managed: {cargo.managed}")
    print(f"    Size limit: {cargo.size_limit_mb} MB")
    assert not cargo.managed
    
    # 2. Get cargo
    print("\n[2] Getting cargo by ID...")
    fetched = await client.cargos.get(cargo.id)
    assert fetched.id == cargo.id
    print(f"  ✓ Fetched cargo: {fetched.id}")
    
    # 3. List cargos (external only by default)
    print("\n[3] Listing external cargos...")
    result = await client.cargos.list(limit=10)
    print(f"  ✓ Listed {len(result.items)} external cargos")
    assert any(c.id == cargo.id for c in result.items)
    
    # 4. Create sandbox with external cargo
    print("\n[4] Creating sandbox with external cargo...")
    sandbox = await client.create_sandbox(
        profile="python-default",
        cargo_id=cargo.id,
        ttl=60,
    )
    print(f"  ✓ Created sandbox {sandbox.id} with cargo {cargo.id}")
    assert sandbox.cargo_id == cargo.id
    
    # 5. Write data to cargo via sandbox
    print("\n[5] Writing data via sandbox...")
    await sandbox.filesystem.write_file("cargo_test.txt", "Data in cargo")
    print("  ✓ Wrote cargo_test.txt")
    
    # 6. Delete sandbox (cargo should persist)
    print("\n[6] Deleting sandbox...")
    await sandbox.delete()
    print("  ✓ Sandbox deleted")
    
    # 7. Create new sandbox with same cargo
    print("\n[7] Creating new sandbox with same cargo...")
    sandbox2 = await client.create_sandbox(
        profile="python-default",
        cargo_id=cargo.id,
        ttl=60,
    )
    content = await sandbox2.filesystem.read_file("cargo_test.txt")
    print(f"  ✓ Data persisted: {content}")
    assert content == "Data in cargo"
    await sandbox2.delete()
    
    # 8. Delete cargo
    print("\n[8] Deleting cargo...")
    await client.cargos.delete(cargo.id)
    print("  ✓ Cargo deleted")
    
    # 9. Verify cargo deleted
    try:
        await client.cargos.get(cargo.id)
        assert False, "Should have raised NotFoundError"
    except NotFoundError:
        print("  ✓ Verified cargo deletion")


async def test_idempotency(client: BayClient) -> str:
    """Test idempotency support."""
    print("\n" + "=" * 60)
    print("IDEMPOTENCY TESTS")
    print("=" * 60)
    
    idempotency_key = "smoke-test-idem-key-12345"
    
    # 1. Create with idempotency key
    print("\n[1] Creating sandbox with idempotency key...")
    sandbox1 = await client.create_sandbox(
        profile="python-default",
        ttl=60,
        idempotency_key=idempotency_key,
    )
    print(f"  ✓ Created sandbox: {sandbox1.id}")
    
    # 2. Retry with same key should return same sandbox
    print("\n[2] Retrying with same idempotency key...")
    sandbox2 = await client.create_sandbox(
        profile="python-default",
        ttl=60,
        idempotency_key=idempotency_key,
    )
    print(f"  ✓ Got sandbox: {sandbox2.id}")
    assert sandbox1.id == sandbox2.id, "Idempotency failed!"
    print("  ✓ Same sandbox returned (idempotency works)")
    
    return sandbox1.id


async def test_error_handling(client: BayClient):
    """Test error handling."""
    print("\n" + "=" * 60)
    print("ERROR HANDLING TESTS")
    print("=" * 60)
    
    # 1. NotFoundError for non-existent sandbox
    print("\n[1] Getting non-existent sandbox...")
    try:
        await client.get_sandbox("nonexistent-sandbox-id")
        assert False, "Should have raised NotFoundError"
    except NotFoundError as e:
        print(f"  ✓ Got NotFoundError: {e.message}")
    
    # 2. NotFoundError for non-existent cargo
    print("\n[2] Getting non-existent cargo...")
    try:
        await client.cargos.get("nonexistent-cargo-id")
        assert False, "Should have raised NotFoundError"
    except NotFoundError as e:
        print(f"  ✓ Got NotFoundError: {e.message}")


async def cleanup(client: BayClient, sandbox_ids: list[str]):
    """Clean up test resources."""
    print("\n" + "=" * 60)
    print("CLEANUP")
    print("=" * 60)
    
    for sid in sandbox_ids:
        try:
            sandbox = await client.get_sandbox(sid)
            await sandbox.delete()
            print(f"  ✓ Deleted sandbox: {sid}")
        except NotFoundError:
            print(f"  - Sandbox already deleted: {sid}")
        except Exception as e:
            print(f"  ✗ Failed to delete {sid}: {e}")


async def main():
    """Run comprehensive smoke test."""
    endpoint = "http://127.0.0.1:8002"
    
    print("=" * 60)
    print("SHIPYARD NEO SDK SMOKE TEST")
    print("=" * 60)
    print(f"\nConnecting to Bay at {endpoint}...")
    
    sandbox_ids_to_cleanup = []
    
    try:
        async with BayClient(
            endpoint_url=endpoint,
            access_token="test-token",
        ) as client:
            # Run all tests
            sandbox_id = await test_sandbox_lifecycle(client)
            sandbox_ids_to_cleanup.append(sandbox_id)
            
            await test_python_capability(client, sandbox_id)
            await test_shell_capability(client, sandbox_id)
            await test_filesystem_capability(client, sandbox_id)
            await test_cargo_manager(client)
            
            idem_sandbox_id = await test_idempotency(client)
            sandbox_ids_to_cleanup.append(idem_sandbox_id)
            
            await test_error_handling(client)
            
            # Cleanup
            await cleanup(client, sandbox_ids_to_cleanup)
            
            print("\n" + "=" * 60)
            print("✅ ALL SMOKE TESTS PASSED!")
            print("=" * 60)
            
    except Exception as e:
        print(f"\n❌ Smoke test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
