"""Interactive Data Analysis (Jupyter-style) workflow tests.

Purpose: Simulate a data analyst's workflow:
- Multi-round code execution with variable persistence within session
- File upload/download
- Stop/resume with file persistence but variable loss

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 1

Note: workflow 场景测试默认会被 SERIAL_GROUPS["workflows"] 归类为 serial/workflows，
在“两阶段”执行流程的 Phase 2 独占 Bay 跑。
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestInteractiveDataAnalysisWorkflow:
    """Interactive Data Analysis (Jupyter-style workflow)."""

    async def test_multi_round_execution_preserves_variables(self):
        """Variables should persist across multiple exec calls in the same session."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Round 1: Define a variable
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "x = 42", "timeout": 30},
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                assert exec1.json()["success"] is True

                # Round 2: Use the variable
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(x * 2)", "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True
                assert "84" in result2["output"], (
                    f"Expected '84' in output, got: {result2['output']}"
                )

                # Round 3: Define another variable using the first
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "y = x + 8; print(y)", "timeout": 30},
                    timeout=30.0,
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                assert result3["success"] is True
                assert "50" in result3["output"], (
                    f"Expected '50' in output, got: {result3['output']}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_stop_resume_loses_variables_but_keeps_files(self):
        """After stop/resume, variables are lost but files persist."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Define a variable
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "my_data = 'important_value'", "timeout": 30},
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                assert exec1.json()["success"] is True

                # Write a file
                write_response = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "data.txt", "content": "file_content_here"},
                    timeout=30.0,
                )
                assert write_response.status_code == 200

                # Verify variable exists before stop
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(my_data)", "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                assert exec2.json()["success"] is True
                assert "important_value" in exec2.json()["output"]

                # Stop sandbox
                stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert stop_response.status_code == 200

                # Verify status is idle
                get_response = await client.get(f"/v1/sandboxes/{sandbox_id}")
                assert get_response.status_code == 200
                assert get_response.json()["status"] == "idle"

                # Resume by executing code - variable should be lost
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "print(my_data)", "timeout": 30},
                    timeout=120.0,  # Cold start
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                # Should fail - variable not defined in new session
                assert result3["success"] is False, "Expected NameError after stop/resume"
                assert "NameError" in (result3.get("error") or ""), (
                    f"Expected NameError, got: {result3}"
                )

                # But file should still exist
                read_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "data.txt"},
                    timeout=30.0,
                )
                assert read_response.status_code == 200
                assert read_response.json()["content"] == "file_content_here"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_file_created_in_code_persists_after_stop(self):
        """Files created via Python code should persist after stop/resume."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Create a file using Python code
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": "with open('output.txt', 'w') as f: f.write('generated_content')",
                        "timeout": 30,
                    },
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                assert exec1.json()["success"] is True

                # Verify file exists
                read_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "output.txt"},
                    timeout=30.0,
                )
                assert read_response.status_code == 200
                assert read_response.json()["content"] == "generated_content"

                # Stop sandbox
                stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert stop_response.status_code == 200

                # Resume and verify file still exists
                read_response2 = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "output.txt"},
                    timeout=120.0,  # May trigger cold start
                )
                assert read_response2.status_code == 200
                assert read_response2.json()["content"] == "generated_content"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_upload_process_download_workflow(self):
        """Complete workflow: upload data -> process -> download result."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Upload a data file via filesystem API
                data_content = "apple,10\nbanana,20\norange,15"
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "data.csv", "content": data_content},
                    timeout=120.0,
                )

                # Process data using Python
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": """
# Read and process data
with open('data.csv', 'r') as f:
    lines = f.readlines()

total = 0
for line in lines:
    parts = line.strip().split(',')
    if len(parts) == 2:
        total += int(parts[1])

# Write result
with open('result.txt', 'w') as f:
    f.write(f'Total: {total}')

print(f'Processed {len(lines)} items, total = {total}')
""",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is True
                assert "45" in result1["output"], f"Expected total 45, got: {result1['output']}"

                # Download result file
                download_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/download",
                    params={"path": "result.txt"},
                    timeout=30.0,
                )
                assert download_response.status_code == 200
                assert b"Total: 45" in download_response.content

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
