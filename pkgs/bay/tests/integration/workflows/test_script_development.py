"""Script Development and Debugging workflow tests.

Purpose: Simulate a developer's workflow for writing and debugging Python scripts:
- File creation, modification, and overwrite
- Execution failure error handling with traceback
- Successful execution with output

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 2

Note: workflow 场景测试默认会被 SERIAL_GROUPS["workflows"] 归类为 serial/workflows，
在“两阶段”执行流程的 Phase 2 独占 Bay 跑。
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestScriptDevelopmentWorkflow:
    """Script Development and Debugging (IDE-style workflow)."""

    async def test_execution_failure_returns_error_traceback(self):
        """Executing buggy code should return success=false with traceback."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write a buggy script
                buggy_script = "print(1/0)"  # ZeroDivisionError
                write_response = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "script.py", "content": buggy_script},
                    timeout=120.0,  # First file op triggers container startup
                )
                assert write_response.status_code == 200

                # Execute the buggy script
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('script.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()

                # Should fail with ZeroDivisionError
                assert result["success"] is False, (
                    f"Expected execution to fail, but got success: {result}"
                )
                assert "ZeroDivisionError" in (result.get("error") or ""), (
                    f"Expected ZeroDivisionError in error, got: {result}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_file_overwrite_updates_content(self):
        """PUT to same path should overwrite file content."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write version 1 (buggy)
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "script.py", "content": "print(1/0)"},
                    timeout=120.0,
                )

                # Overwrite with version 2 (fixed)
                fixed_script = "print('Hello, World!')"
                write_response = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "script.py", "content": fixed_script},
                    timeout=30.0,
                )
                assert write_response.status_code == 200

                # Read file to verify overwrite
                read_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "script.py"},
                    timeout=30.0,
                )
                assert read_response.status_code == 200
                assert read_response.json()["content"] == fixed_script

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_fix_and_rerun_workflow(self):
        """Complete workflow: write buggy code -> fail -> fix -> succeed."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Step 1: Write buggy script
                buggy_script = "print(1/0)"
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "script.py", "content": buggy_script},
                    timeout=120.0,
                )

                # Step 2: Execute - should fail
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('script.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is False
                assert "ZeroDivisionError" in (result1.get("error") or "")

                # Step 3: Fix the script
                fixed_script = "print('Hello, World!')"
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "script.py", "content": fixed_script},
                    timeout=30.0,
                )

                # Step 4: Execute again - should succeed
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('script.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True, f"Expected success, got: {result2}"
                assert "Hello, World!" in result2["output"], (
                    f"Expected 'Hello, World!' in output, got: {result2['output']}"
                )

                # Step 5: Verify file content is the fixed version
                read_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "script.py"},
                    timeout=30.0,
                )
                assert read_response.status_code == 200
                assert read_response.json()["content"] == fixed_script

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_syntax_error_handling(self):
        """Script with syntax error should return error with traceback."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write script with syntax error
                syntax_error_script = "print('unclosed string"
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "bad_syntax.py", "content": syntax_error_script},
                    timeout=120.0,
                )

                # Execute - should fail with SyntaxError
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('bad_syntax.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()

                assert result["success"] is False
                # Should contain SyntaxError or related error
                error_text = result.get("error") or ""
                assert "SyntaxError" in error_text or "EOL" in error_text, (
                    f"Expected SyntaxError, got: {error_text}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_import_error_handling(self):
        """Script with import error should return error with traceback."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write script that imports non-existent module
                import_error_script = "import nonexistent_module_12345"
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "import_error.py", "content": import_error_script},
                    timeout=120.0,
                )

                # Execute - should fail with ModuleNotFoundError
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('import_error.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()

                assert result["success"] is False
                error_text = result.get("error") or ""
                assert "ModuleNotFoundError" in error_text or "ImportError" in error_text, (
                    f"Expected ModuleNotFoundError, got: {error_text}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_multiple_files_workflow(self):
        """Workflow with multiple script files and imports."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write utility module
                utils_script = """
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "utils.py", "content": utils_script},
                    timeout=120.0,
                )

                # Write main script that imports utils
                main_script = """
from utils import add, multiply
result = add(3, multiply(4, 5))
print(f"Result: {result}")
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "main.py", "content": main_script},
                    timeout=30.0,
                )

                # Execute main script
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('main.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()

                assert result["success"] is True, f"Expected success, got: {result}"
                # 3 + (4 * 5) = 3 + 20 = 23
                assert "23" in result["output"], f"Expected '23' in output, got: {result['output']}"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
