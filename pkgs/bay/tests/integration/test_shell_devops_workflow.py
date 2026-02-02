"""E2E tests for Shell-driven DevOps automation workflow.

Scenario 8: Shell 驱动的 DevOps 自动化 (CI/CD 风格)

User persona: DevOps engineer / automation script that primarily uses shell commands
for build, test, and deployment tasks.

Goal: Verify shell-centric workflows including:
- Basic shell command execution and output capture
- Using pre-installed container tools (git, node, curl, etc.)
- Working directory switching (cwd parameter)
- Multi-step build processes
- Exit code handling and error detection

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 8
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestShellDevOpsWorkflowE2E:
    """E2E tests for DevOps-style shell workflows.
    
    Simulates a CI/CD pipeline with:
    - Pre-installed tool verification
    - Node.js project build
    - Git workflow
    - File packaging and download
    """

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    # --- Part B: Verify Pre-installed Tools ---

    async def test_python_version_available(self, sandbox_id: str):
        """Verify Python 3 is available in the container."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "python3 --version"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Python 3" in result["output"]

    async def test_node_version_available(self, sandbox_id: str):
        """Verify Node.js is available in the container."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "node --version"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Node.js version should start with 'v'
            assert result["output"].strip().startswith("v")

    async def test_npm_and_pnpm_available(self, sandbox_id: str):
        """Verify npm and pnpm package managers are available."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "npm --version && pnpm --version"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Should have version numbers in output

    async def test_git_available(self, sandbox_id: str):
        """Verify git is available in the container."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "git --version"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "git version" in result["output"]

    async def test_curl_available(self, sandbox_id: str):
        """Verify curl is available in the container."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "curl --version | head -1"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "curl" in result["output"]


class TestNodeJsProjectBuildE2E:
    """E2E tests for Node.js project build workflow."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_nodejs_project_workflow(self, sandbox_id: str):
        """Complete Node.js project build workflow.
        
        Steps:
        1. Create package.json
        2. Create index.js
        3. Run npm build script
        4. Run the Node.js application
        5. Run npm test
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Step 1: Create package.json
            package_json = """{
  "name": "myapp",
  "version": "1.0.0",
  "scripts": {
    "build": "echo 'Building...' && node -e \\"console.log('Build complete!')\\"",
    "test": "echo 'Running tests...' && exit 0"
  }
}"""
            response = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "myapp/package.json", "content": package_json},
                timeout=120.0,
            )
            assert response.status_code == 200

            # Step 2: Create index.js
            index_js = """console.log('Hello from Node.js!');
console.log('Node version:', process.version);"""
            response = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "myapp/index.js", "content": index_js},
                timeout=30.0,
            )
            assert response.status_code == 200

            # Step 3: Run npm build in project directory
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "npm run build", "cwd": "myapp"},
                timeout=60.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Building" in result["output"] or "Build complete" in result["output"]

            # Step 4: Run Node.js application
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "node index.js", "cwd": "myapp"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Hello from Node.js" in result["output"]
            assert "Node version" in result["output"]

            # Step 5: Run npm test
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "npm test", "cwd": "myapp"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Running tests" in result["output"]


class TestGitWorkflowE2E:
    """E2E tests for Git workflow."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_git_init_and_commit_workflow(self, sandbox_id: str):
        """Complete Git init and commit workflow.
        
        Steps:
        1. git init
        2. Configure git user
        3. Create file and commit
        4. View git log
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Step 1: Initialize git repository
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "git init myrepo"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Initialized" in result["output"] or "git" in result["output"].lower()

            # Step 2: Configure git user
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "git config user.email 'test@example.com' && git config user.name 'Test User'",
                    "cwd": "myrepo"
                },
                timeout=30.0,
            )
            assert response.status_code == 200
            assert response.json()["success"] is True

            # Step 3: Create file and commit
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "echo 'Hello Git' > README.md && git add . && git commit -m 'Initial commit'",
                    "cwd": "myrepo"
                },
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Initial commit" in result["output"]

            # Step 4: View git log
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "git log --oneline", "cwd": "myrepo"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "Initial commit" in result["output"]


class TestShellPipeAndTextProcessingE2E:
    """E2E tests for shell pipes and text processing."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_grep_in_pipeline(self, sandbox_id: str):
        """Grep in pipeline should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo -e 'line1\\nline2\\nline3' | grep line2"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "line2" in result["output"]

    async def test_awk_text_processing(self, sandbox_id: str):
        """awk text processing should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo 'hello world' | awk '{print $2}'"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "world" in result["output"]

    async def test_sed_substitution(self, sandbox_id: str):
        """sed substitution should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo 'hello' | sed 's/hello/goodbye/'"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "goodbye" in result["output"]

    async def test_find_command(self, sandbox_id: str):
        """find command should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create a test file first
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "findtest/test.js", "content": "// test"},
                timeout=120.0,
            )

            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "find . -name '*.js'"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "test.js" in result["output"]


class TestShellErrorHandlingE2E:
    """E2E tests for shell error handling and exit codes."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_nonexistent_command_fails(self, sandbox_id: str):
        """Non-existent command should return success=False."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "nonexistent_command_12345"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is False
            # Exit code 127 is typically "command not found"
            assert result["exit_code"] != 0

    async def test_explicit_nonzero_exit_code(self, sandbox_id: str):
        """Explicit non-zero exit code should be captured."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "exit 42"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is False
            assert result["exit_code"] == 42

    async def test_grep_no_match_returns_exit_1(self, sandbox_id: str):
        """grep with no match should return exit code 1."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo 'hello' | grep 'xyz'"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is False
            assert result["exit_code"] == 1


class TestPackageAndDownloadE2E:
    """E2E tests for file packaging and download."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_tar_package_and_download(self, sandbox_id: str):
        """Create tar archive and download it.
        
        Steps:
        1. Create project files
        2. Create tar.gz archive
        3. Verify archive contents
        4. Download the archive
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Step 1: Create project files
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "project/file1.txt", "content": "content1"},
                timeout=120.0,
            )
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "project/file2.txt", "content": "content2"},
                timeout=30.0,
            )

            # Step 2: Create tar archive
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "tar -czvf project.tar.gz project/"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            # Output should show files being added
            assert "project/" in result["output"]

            # Step 3: Verify archive contents
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "tar -tzvf project.tar.gz"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "file1.txt" in result["output"]
            assert "file2.txt" in result["output"]

            # Step 4: Download the archive
            response = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/download",
                params={"path": "project.tar.gz"},
                timeout=30.0,
            )
            assert response.status_code == 200
            # Should return binary content (tar.gz is binary)
            assert len(response.content) > 0
            # tar.gz magic bytes: 1f 8b
            assert response.content[:2] == b'\x1f\x8b'


class TestShellWorkingDirectoryE2E:
    """E2E tests for shell working directory (cwd) handling."""

    @pytest.fixture
    async def sandbox_id(self):
        """Create a sandbox for testing and clean up after."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_response = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_response.status_code == 201
            sandbox_id = create_response.json()["id"]
            
            yield sandbox_id
            
            await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_default_cwd_is_workspace(self, sandbox_id: str):
        """Default working directory should be /workspace."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "pwd"},
                timeout=120.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "/workspace" in result["output"]

    async def test_relative_cwd_changes_directory(self, sandbox_id: str):
        """Relative cwd should change to that directory within /workspace."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create a subdirectory
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "subdir/test.txt", "content": "test"},
                timeout=120.0,
            )

            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "pwd", "cwd": "subdir"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "subdir" in result["output"]

    async def test_nested_cwd_works(self, sandbox_id: str):
        """Nested cwd path should work."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            # Create nested directory
            await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "a/b/c/test.txt", "content": "test"},
                timeout=120.0,
            )

            response = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "pwd", "cwd": "a/b/c"},
                timeout=30.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "a/b/c" in result["output"]
