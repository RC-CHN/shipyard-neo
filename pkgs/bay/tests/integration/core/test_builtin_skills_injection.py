"""Built-in skills injection E2E tests.

Purpose: Verify that Ship's entrypoint.sh correctly injects built-in skills
into /workspace/skills/ when a sandbox container starts.

Test scenarios:
1. Skills are injected into /workspace/skills/<skill_name>/ on first exec
2. SKILL.md contains valid frontmatter (name + description)
3. Skills persist across stop/resume cycles (workspace is on Cargo Volume)
4. Custom (agent-created) skills in skills/ are NOT overwritten by injection
5. Skills are idempotent - re-injection produces same result

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_SHIP_SKILL = "python-sandbox"
SKILL_MARKER = "skills"


async def _exec_shell(
    client: httpx.AsyncClient,
    sandbox_id: str,
    command: str,
    *,
    timeout: int = 30,
) -> dict:
    """Execute shell command in sandbox and return parsed response."""
    resp = await client.post(
        f"/v1/sandboxes/{sandbox_id}/shell/exec",
        json={"command": command, "timeout": timeout},
        timeout=120.0,
    )
    assert resp.status_code == 200, f"Shell exec failed: {resp.text}"
    return resp.json()


async def _exec_python(
    client: httpx.AsyncClient,
    sandbox_id: str,
    code: str,
    *,
    timeout: int = 30,
) -> dict:
    """Execute Python code in sandbox and return parsed response."""
    resp = await client.post(
        f"/v1/sandboxes/{sandbox_id}/python/exec",
        json={"code": code, "timeout": timeout},
        timeout=120.0,
    )
    assert resp.status_code == 200, f"Python exec failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_builtin_skills_injected_on_first_exec():
    """After first exec (container startup), skills/python-sandbox/SKILL.md exists."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # First exec triggers container startup → entrypoint.sh runs injection
            result = await _exec_shell(client, sid, "echo 'trigger startup'")
            assert result["success"] is True

            # Verify skills directory exists
            result = await _exec_shell(
                client,
                sid,
                f"test -d /workspace/{SKILL_MARKER} && echo EXISTS",
            )
            assert result["success"] is True
            assert "EXISTS" in result["output"]

            # Verify skill directory exists
            result = await _exec_shell(
                client,
                sid,
                f"test -f /workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md && echo FOUND",
            )
            assert result["success"] is True, (
                f"SKILL.md not found at /workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md: "
                f"{result}"
            )
            assert "FOUND" in result["output"]


async def test_skill_md_has_valid_frontmatter():
    """Injected SKILL.md contains YAML frontmatter with name and description."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # Read SKILL.md content via filesystem API
            # First trigger container startup
            await _exec_shell(client, sid, "echo 'startup'")

            # Read the injected SKILL.md
            read_resp = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": f"{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md"},
                timeout=30.0,
            )
            assert read_resp.status_code == 200, f"Failed to read SKILL.md: {read_resp.text}"
            content = read_resp.json()["content"]

            # Verify frontmatter structure
            assert content.startswith("---"), (
                "SKILL.md should start with YAML frontmatter delimiter"
            )
            assert "name:" in content, "Frontmatter should contain 'name:' field"
            assert "description:" in content, "Frontmatter should contain 'description:' field"
            assert "python-sandbox" in content, "Frontmatter name should be 'python-sandbox'"


async def test_skill_directory_is_flat_namespace():
    """Skills are injected at /workspace/skills/<skill_name>/ (flat, no runtime subdirectory)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # Trigger startup
            await _exec_shell(client, sid, "echo 'startup'")

            # List skills/ directory - should contain skill name directly, not ship/gull subdirs
            list_resp = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/directories",
                params={"path": SKILL_MARKER},
                timeout=30.0,
            )
            assert list_resp.status_code == 200
            entries = list_resp.json().get("entries", [])
            names = [e["name"] for e in entries]

            # python-sandbox should be a direct child of skills/
            assert EXPECTED_SHIP_SKILL in names, (
                f"Expected '{EXPECTED_SHIP_SKILL}' in skills/ entries, got: {names}"
            )

            # Should NOT have runtime-namespaced directories
            assert "ship" not in names, (
                f"Should not have 'ship/' subdirectory in skills/ (flat namespace), got: {names}"
            )


async def test_skills_persist_across_stop_resume():
    """Skills on Cargo Volume persist after stop and resume."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=60.0
    ) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # First exec → skills are injected
            result = await _exec_shell(client, sid, "echo 'first session'")
            assert result["success"] is True

            # Verify skills exist
            result = await _exec_shell(
                client,
                sid,
                f"test -f /workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md && echo FOUND",
            )
            assert "FOUND" in result["output"]

            # Stop sandbox (destroys session but keeps Cargo Volume)
            stop_resp = await client.post(f"/v1/sandboxes/{sid}/stop", timeout=60.0)
            assert stop_resp.status_code == 200

            # Resume with new exec → cold start again
            result = await _exec_shell(
                client,
                sid,
                f"test -f /workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}"
                f"/SKILL.md && echo STILL_THERE",
            )
            assert result["success"] is True
            assert "STILL_THERE" in result["output"], (
                f"Skills should persist after stop/resume, output: {result['output']}"
            )


async def test_custom_skills_not_overwritten_by_injection():
    """Agent-created custom skills in skills/ are NOT removed by Ship's injection."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=60.0
    ) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # First exec → skills are injected
            await _exec_shell(client, sid, "echo 'startup'")

            # Create a custom skill (simulating what an agent/MCP layer would do)
            custom_skill_content = """---
name: my-custom-skill
description: "A custom agent skill"
---

# My Custom Skill

This is a custom skill created by the agent.
"""
            write_resp = await client.put(
                f"/v1/sandboxes/{sid}/filesystem/files",
                json={
                    "path": f"{SKILL_MARKER}/my-custom-skill/SKILL.md",
                    "content": custom_skill_content,
                },
                timeout=30.0,
            )
            assert write_resp.status_code == 200

            # Stop and resume → entrypoint.sh runs again, only overwrites built-in skills
            stop_resp = await client.post(f"/v1/sandboxes/{sid}/stop", timeout=60.0)
            assert stop_resp.status_code == 200

            # Resume
            result = await _exec_shell(client, sid, "echo 'resumed'")
            assert result["success"] is True

            # Verify custom skill is still there
            read_resp = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": f"{SKILL_MARKER}/my-custom-skill/SKILL.md"},
                timeout=30.0,
            )
            assert read_resp.status_code == 200, (
                f"Custom skill should survive re-injection, got {read_resp.status_code}"
            )
            assert "my-custom-skill" in read_resp.json()["content"]

            # Also verify built-in skill is still there (re-injected)
            result = await _exec_shell(
                client,
                sid,
                f"test -f /workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md && echo FOUND",
            )
            assert "FOUND" in result["output"]


async def test_skill_injection_is_idempotent():
    """Repeated container starts produce the same skill content (overwrite semantics)."""
    async with httpx.AsyncClient(
        base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=60.0
    ) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # First start
            await _exec_shell(client, sid, "echo 'start 1'")

            # Read skill content
            read1 = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": f"{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md"},
                timeout=30.0,
            )
            assert read1.status_code == 200
            content1 = read1.json()["content"]

            # Stop and start again
            await client.post(f"/v1/sandboxes/{sid}/stop", timeout=60.0)
            await _exec_shell(client, sid, "echo 'start 2'")

            # Read skill content again
            read2 = await client.get(
                f"/v1/sandboxes/{sid}/filesystem/files",
                params={"path": f"{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md"},
                timeout=30.0,
            )
            assert read2.status_code == 200
            content2 = read2.json()["content"]

            # Content should be identical
            assert content1 == content2, (
                "Skill content should be identical after re-injection (idempotent)"
            )


async def test_skill_readable_by_python():
    """Skills can be read and parsed by Python code inside the sandbox."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # Trigger startup
            await _exec_shell(client, sid, "echo 'startup'")

            # Use Python to read and parse the skill
            parse_code = f"""
import os
import re

skill_path = '/workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md'
assert os.path.isfile(skill_path), f"SKILL.md not found at {{skill_path}}"

with open(skill_path) as f:
    text = f.read()

# Parse frontmatter
match = re.match(r'^---\\s*\\n(.*?)\\n---', text, re.DOTALL)
assert match, "No YAML frontmatter found"

meta = {{}}
for line in match.group(1).splitlines():
    if ':' in line:
        key, _, value = line.partition(':')
        meta[key.strip()] = value.strip().strip("'\\"")

print(f"name={{meta.get('name', 'MISSING')}}")
print(f"has_description={{bool(meta.get('description'))}}")
print("PARSE_OK")
"""
            result = await _exec_python(client, sid, parse_code)
            assert result["success"] is True, f"Python parse failed: {result}"
            assert "name=python-sandbox" in result["output"]
            assert "has_description=True" in result["output"]
            assert "PARSE_OK" in result["output"]


async def test_skills_owned_by_shipyard_user():
    """Injected skill files are owned by shipyard user (not root)."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sid = sandbox["id"]

            # Trigger startup
            await _exec_shell(client, sid, "echo 'startup'")

            # Check file ownership
            result = await _exec_shell(
                client,
                sid,
                f"stat -c '%U' /workspace/{SKILL_MARKER}/{EXPECTED_SHIP_SKILL}/SKILL.md",
            )
            assert result["success"] is True
            assert "shipyard" in result["output"], (
                f"Skills should be owned by 'shipyard', got: {result['output']}"
            )
