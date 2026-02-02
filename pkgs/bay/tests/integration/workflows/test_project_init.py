"""Project Initialization and Dependency Installation workflow tests.

Purpose: Simulate a software engineer's workflow for setting up a project:
- Multi-file/nested directory creation
- Dependency installation persistence boundaries
- pip install --target for workspace-persistent dependencies

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 3

Note: workflow 场景测试默认会被 SERIAL_GROUPS["workflows"] 归类为 serial/workflows，
在“两阶段”执行流程的 Phase 2 独占 Bay 跑。
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestProjectInitializationWorkflow:
    """Project Initialization and Dependency Installation."""

    async def test_nested_directory_auto_creation(self):
        """PUT to nested path should auto-create parent directories."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write to deeply nested path (mkdir -p semantics)
                nested_content = "print('Hello from nested!')"
                write_response = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "src/core/main.py", "content": nested_content},
                    timeout=120.0,  # First op triggers container startup
                )
                assert write_response.status_code == 200

                # Read it back to verify
                read_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "src/core/main.py"},
                    timeout=30.0,
                )
                assert read_response.status_code == 200
                assert read_response.json()["content"] == nested_content

                # Verify directory structure by listing parent
                list_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/directories",
                    params={"path": "src"},
                    timeout=30.0,
                )
                assert list_response.status_code == 200
                entries = list_response.json().get("entries", [])
                names = [e.get("name") for e in entries]
                assert "core" in names, f"Expected 'core' in {names}"

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_requirements_file_workflow(self):
        """Write requirements.txt and verify content."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Write requirements.txt
                requirements_content = "requests==2.31.0\npandas>=2.0.0"
                write_response = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "requirements.txt", "content": requirements_content},
                    timeout=120.0,
                )
                assert write_response.status_code == 200

                # Read it back
                read_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "requirements.txt"},
                    timeout=30.0,
                )
                assert read_response.status_code == 200
                assert read_response.json()["content"] == requirements_content

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_pip_install_target_persists_after_stop(self):
        """Dependencies installed with --target to workspace persist after stop/resume."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Install a small package to /workspace/.libs
                # Using 'six' as it's small and has no dependencies
                install_code = """
import subprocess
import sys

# Install to /workspace/.libs
result = subprocess.run(
    [sys.executable, '-m', 'pip', 'install', 'six', '--target', '/workspace/.libs', '--quiet'],
    capture_output=True,
    text=True
)
print(f"Install exit code: {result.returncode}")
if result.returncode != 0:
    print(f"stderr: {result.stderr}")
"""
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": install_code, "timeout": 120},
                    timeout=180.0,  # Pip install can take time
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is True, f"Install failed: {result1}"
                assert "exit code: 0" in result1["output"], f"Pip install failed: {result1}"

                # Verify the package can be imported
                verify_code = """
import sys
sys.path.insert(0, '/workspace/.libs')
import six
print(f"six version: {six.__version__}")
"""
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": verify_code, "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True, f"Import failed: {result2}"
                assert "six version:" in result2["output"]

                # Stop sandbox
                stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert stop_response.status_code == 200

                # Resume and verify package is still available
                verify_after_stop_code = """
import sys
sys.path.insert(0, '/workspace/.libs')
import six
print(f"six still available: {six.__version__}")
"""
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": verify_after_stop_code, "timeout": 30},
                    timeout=120.0,  # Cold start
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                assert result3["success"] is True, (
                    f"Package not available after stop/resume: {result3}"
                )
                assert "six still available:" in result3["output"], (
                    f"Expected 'six still available:', got: {result3['output']}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_standard_pip_install_not_persisted(self):
        """Standard pip install (without --target) should NOT persist after stop."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Install a package the standard way (to system dir)
                # Using 'toml' as it's small
                install_code = """
import subprocess
import sys

result = subprocess.run(
    [sys.executable, '-m', 'pip', 'install', 'toml', '--quiet'],
    capture_output=True,
    text=True
)
print(f"Install exit code: {result.returncode}")
"""
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": install_code, "timeout": 120},
                    timeout=180.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is True, f"Install failed: {result1}"

                # Verify it works in current session
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": "import toml; print(f'toml version: {toml.__version__}')",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                assert exec2.json()["success"] is True

                # Stop sandbox
                stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert stop_response.status_code == 200

                # Resume - package should NOT be available
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "import toml; print('toml available')", "timeout": 30},
                    timeout=120.0,  # Cold start
                )
                assert exec3.status_code == 200
                result3 = exec3.json()

                # Should fail with ModuleNotFoundError
                assert result3["success"] is False, (
                    f"Expected toml to NOT be available after stop, but got: {result3}"
                )
                assert "ModuleNotFoundError" in (result3.get("error") or ""), (
                    f"Expected ModuleNotFoundError, got: {result3}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_multi_file_project_structure(self):
        """Create a complete project structure with multiple files."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Create requirements.txt
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "requirements.txt", "content": "# No dependencies for this test"},
                    timeout=120.0,
                )

                # Create src/__init__.py
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "src/__init__.py", "content": ""},
                    timeout=30.0,
                )

                # Create src/utils.py
                utils_content = """
def greet(name):
    return f"Hello, {name}!"

def add(a, b):
    return a + b
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "src/utils.py", "content": utils_content},
                    timeout=30.0,
                )

                # Create src/main.py
                main_content = """
import sys
sys.path.insert(0, '/workspace')
from src.utils import greet, add

result = add(10, 20)
message = greet("World")
print(f"{message} Sum is {result}")
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "src/main.py", "content": main_content},
                    timeout=30.0,
                )

                # Create README.md
                readme_content = """# Test Project

This is a test project for E2E testing.
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "README.md", "content": readme_content},
                    timeout=30.0,
                )

                # List root directory to verify structure
                list_response = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/directories",
                    params={"path": "."},
                    timeout=30.0,
                )
                assert list_response.status_code == 200
                entries = list_response.json().get("entries", [])
                names = [e.get("name") for e in entries]
                assert "requirements.txt" in names
                assert "README.md" in names
                assert "src" in names

                # Execute main.py
                exec_response = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('src/main.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec_response.status_code == 200
                result = exec_response.json()
                assert result["success"] is True, f"Execution failed: {result}"
                assert "Hello, World!" in result["output"]
                assert "30" in result["output"]

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_create_and_use_virtualenv_in_workspace(self):
        """Create a virtual environment in workspace for persistent dependencies."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create sandbox
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]

            try:
                # Create a virtualenv in /workspace/.venv
                create_venv_code = """
import subprocess
import sys

result = subprocess.run(
    [sys.executable, '-m', 'venv', '/workspace/.venv'],
    capture_output=True,
    text=True
)
print(f"Venv creation exit code: {result.returncode}")
if result.returncode != 0:
    print(f"stderr: {result.stderr}")
"""
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": create_venv_code, "timeout": 60},
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is True, f"Venv creation failed: {result1}"
                assert "exit code: 0" in result1["output"]

                # Verify venv exists by checking for activate script
                verify_code = """
import os
venv_path = '/workspace/.venv'
activate_path = os.path.join(venv_path, 'bin', 'activate')
python_path = os.path.join(venv_path, 'bin', 'python')

print(f"Venv exists: {os.path.isdir(venv_path)}")
print(f"Activate exists: {os.path.isfile(activate_path)}")
print(f"Python exists: {os.path.isfile(python_path)}")
"""
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": verify_code, "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True
                assert "Venv exists: True" in result2["output"]
                assert "Python exists: True" in result2["output"]

                # Stop and resume
                stop_response = await client.post(f"/v1/sandboxes/{sandbox_id}/stop")
                assert stop_response.status_code == 200

                # Verify venv still exists after resume
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": verify_code, "timeout": 30},
                    timeout=120.0,
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                assert result3["success"] is True
                assert "Venv exists: True" in result3["output"], (
                    f"Venv not persisted: {result3['output']}"
                )

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
