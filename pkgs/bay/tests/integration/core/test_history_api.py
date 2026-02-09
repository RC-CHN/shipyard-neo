"""Execution history API integration tests.

Purpose: Verify automatic execution evidence recording and history query APIs.

Parallel-safe: Yes - each test creates/deletes its own sandbox.
"""

from __future__ import annotations

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, create_sandbox, e2e_skipif_marks

pytestmark = e2e_skipif_marks


async def test_python_and_shell_exec_record_history_with_metadata():
    """Execution endpoints should persist history entries and return execution metadata."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            py_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={
                    "code": "print('history-python')",
                    "include_code": True,
                    "description": "python smoke",
                    "tags": "python,smoke",
                },
                timeout=120.0,
            )
            assert py_resp.status_code == 200
            py_data = py_resp.json()
            assert py_data["success"] is True
            assert py_data["execution_id"].startswith("exec-")
            assert py_data["execution_time_ms"] >= 0
            assert py_data["code"] == "print('history-python')"

            sh_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "echo history-shell",
                    "include_code": True,
                    "description": "shell smoke",
                    "tags": "shell,smoke",
                },
                timeout=120.0,
            )
            assert sh_resp.status_code == 200
            sh_data = sh_resp.json()
            assert sh_data["success"] is True
            assert sh_data["execution_id"].startswith("exec-")
            assert sh_data["execution_time_ms"] >= 0
            assert sh_data["command"] == "echo history-shell"

            history_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history",
                params={"limit": 10},
            )
            assert history_resp.status_code == 200
            history_data = history_resp.json()
            assert history_data["total"] == 2

            entry_ids = {entry["id"] for entry in history_data["entries"]}
            assert py_data["execution_id"] in entry_ids
            assert sh_data["execution_id"] in entry_ids


async def test_history_filters_get_and_patch_annotation():
    """History endpoints should support filter/query/get/annotation operations."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            py_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={
                    "code": "print('ok')",
                    "description": "alpha run",
                    "tags": "alpha,shared",
                },
                timeout=120.0,
            )
            py_entry_id = py_resp.json()["execution_id"]

            sh_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "sh -c 'echo fail && exit 3'",
                    "tags": "beta,shared",
                },
                timeout=120.0,
            )
            sh_data = sh_resp.json()
            sh_entry_id = sh_data["execution_id"]
            assert sh_data["success"] is False

            success_only_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history",
                params={"success_only": True},
            )
            assert success_only_resp.status_code == 200
            success_entries = success_only_resp.json()["entries"]
            assert len(success_entries) == 1
            assert success_entries[0]["id"] == py_entry_id

            tagged_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history",
                params={"tags": "beta", "exec_type": "shell"},
            )
            assert tagged_resp.status_code == 200
            tagged_data = tagged_resp.json()
            assert tagged_data["total"] == 1
            assert tagged_data["entries"][0]["id"] == sh_entry_id

            patch_resp = await client.patch(
                f"/v1/sandboxes/{sandbox_id}/history/{sh_entry_id}",
                json={"notes": "investigate", "description": "failed shell run"},
            )
            assert patch_resp.status_code == 200
            patched = patch_resp.json()
            assert patched["notes"] == "investigate"
            assert patched["description"] == "failed shell run"

            has_notes_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history",
                params={"has_notes": True},
            )
            assert has_notes_resp.status_code == 200
            noted = has_notes_resp.json()
            assert noted["total"] == 1
            assert noted["entries"][0]["id"] == sh_entry_id

            get_one_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/{sh_entry_id}",
            )
            assert get_one_resp.status_code == 200
            assert get_one_resp.json()["id"] == sh_entry_id


async def test_get_last_execution_supports_type_filter():
    """GET /history/last should return the latest overall or per execution type."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            sh_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={"command": "echo first-shell"},
                timeout=120.0,
            )
            shell_entry_id = sh_resp.json()["execution_id"]

            py_resp = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={"code": "print('second-python')"},
                timeout=120.0,
            )
            python_entry_id = py_resp.json()["execution_id"]

            last_any = await client.get(f"/v1/sandboxes/{sandbox_id}/history/last")
            assert last_any.status_code == 200
            assert last_any.json()["id"] == python_entry_id

            last_shell = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history/last",
                params={"exec_type": "shell"},
            )
            assert last_shell.status_code == 200
            assert last_shell.json()["id"] == shell_entry_id


async def test_history_validation_rejects_invalid_exec_type():
    """History APIs should reject unsupported exec_type values."""
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client) as sandbox:
            sandbox_id = sandbox["id"]

            bad_resp = await client.get(
                f"/v1/sandboxes/{sandbox_id}/history",
                params={"exec_type": "filesystem"},
            )
            assert bad_resp.status_code == 422
