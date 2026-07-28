"""Standalone HTTP contract coverage for Shipyard Neo issue #21.

Run this file directly without Bay, Docker, or a running Ship process:

    uv run --extra test pytest tests/standalone/test_issue_21_filesystem_contract.py

The test mounts Ship's real filesystem router in a temporary FastAPI app.  It
therefore exercises JSON, query-string, and multipart path handling exactly as
the HTTP API does while keeping the test hermetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.workspace as workspace_module
from app.components.filesystem import router as filesystem_router
from app.components.user_manager import run_command


pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class FilesystemContract:
    """Temporary roots and client used by the standalone HTTP contract."""

    client: TestClient
    workspace: Path
    temporary_root: Path


@pytest.fixture
def filesystem_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> FilesystemContract:
    """Build an isolated Ship router with a workspace and temporary root."""
    workspace = tmp_path / "workspace"
    temporary_root = tmp_path / "temporary"
    workspace.mkdir()
    temporary_root.mkdir()
    monkeypatch.setattr(workspace_module, "WORKSPACE_ROOT", workspace)
    monkeypatch.setenv(
        workspace_module.ALLOWED_ROOTS_ENV,
        json.dumps([str(workspace), str(temporary_root)]),
    )

    test_app = FastAPI()
    test_app.include_router(filesystem_router)
    with TestClient(test_app) as client:
        yield FilesystemContract(client, workspace, temporary_root)


def _assert_invalid_path(response, reason: str) -> None:
    """Assert Ship's structured error contract instead of an opaque 500."""
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_path"
    assert detail["details"]["reason"] == reason
    assert detail["details"]["allowed_roots"]


def test_issue_21_unicode_absolute_path_round_trips_all_file_interfaces(
    filesystem_contract: FilesystemContract,
) -> None:
    """Issue #21: absolute Chinese paths work in every HTTP request shape."""
    client = filesystem_contract.client
    filename = "数字 1到10 🧪.txt"
    relative_path = f"reports/{filename}"
    absolute_path = filesystem_contract.workspace / relative_path

    create = client.post(
        "/create_file",
        json={"path": relative_path, "content": "first line\n"},
    )
    assert create.status_code == 200, create.text
    assert create.json()["path"] == str(absolute_path)

    read_relative = client.post("/read_file", json={"path": relative_path})
    assert read_relative.status_code == 200, read_relative.text
    assert read_relative.json()["content"] == "first line\n"

    write_absolute = client.post(
        "/write_file",
        json={"path": str(absolute_path), "content": "second line\n", "mode": "a"},
    )
    assert write_absolute.status_code == 200, write_absolute.text

    edit_absolute = client.post(
        "/edit_file",
        json={
            "path": str(absolute_path),
            "old_string": "second",
            "new_string": "updated",
        },
    )
    assert edit_absolute.status_code == 200, edit_absolute.text

    read_absolute = client.post("/read_file", json={"path": str(absolute_path)})
    assert read_absolute.status_code == 200, read_absolute.text
    assert read_absolute.json()["content"] == "first line\nupdated line\n"

    listed = client.post(
        "/list_dir",
        json={"path": str(absolute_path.parent)},
    )
    assert listed.status_code == 200, listed.text
    assert {item["name"] for item in listed.json()["files"]} == {filename}
    assert listed.json()["files"][0]["path"] == str(absolute_path)

    downloaded = client.get("/download", params={"file_path": str(absolute_path)})
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"first line\nupdated line\n"
    disposition = downloaded.headers["content-disposition"]
    disposition.encode("ascii")
    encoded_filename = quote(filename, safe="!#$&+-.^_`|~")
    assert f"filename*=UTF-8''{encoded_filename}" in disposition

    temporary_name = "临时 文件 🧪.bin"
    temporary_path = filesystem_contract.temporary_root / temporary_name
    binary_content = b"\x00ship\xff"
    uploaded = client.post(
        "/upload",
        data={"file_path": str(temporary_path)},
        files={"file": (temporary_name, binary_content, "application/octet-stream")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["file_path"] == str(temporary_path)

    temporary_list = client.post(
        "/list_dir",
        json={"path": str(filesystem_contract.temporary_root)},
    )
    assert temporary_list.status_code == 200, temporary_list.text
    assert {item["name"] for item in temporary_list.json()["files"]} == {temporary_name}

    temporary_download = client.get(
        "/download",
        params={"file_path": str(temporary_path)},
    )
    assert temporary_download.status_code == 200, temporary_download.text
    assert temporary_download.content == binary_content

    deleted_workspace_file = client.post(
        "/delete_file",
        json={"path": str(absolute_path)},
    )
    assert deleted_workspace_file.status_code == 200, deleted_workspace_file.text
    assert not absolute_path.exists()

    deleted_temporary_file = client.post(
        "/delete_file",
        json={"path": str(temporary_path)},
    )
    assert deleted_temporary_file.status_code == 200, deleted_temporary_file.text
    assert not temporary_path.exists()


def test_contract_rejects_unsafe_paths_with_structured_errors(
    filesystem_contract: FilesystemContract,
) -> None:
    """All JSON, query, and multipart paths share Ship's final boundary."""
    client = filesystem_contract.client
    workspace = filesystem_contract.workspace

    traversal = client.post(
        "/write_file", json={"path": "../outside.txt", "content": "x"}
    )
    _assert_invalid_path(traversal, "path_traversal")

    prefix_spoof = client.get(
        "/download",
        params={"file_path": f"{workspace}-evil/report.txt"},
    )
    _assert_invalid_path(prefix_spoof, "outside_allowed_roots")

    control_character = client.post(
        "/delete_file",
        json={"path": "safe\nname.txt"},
    )
    _assert_invalid_path(control_character, "control_character")

    escaped_target = workspace.parent / "outside"
    escaped_target.mkdir()
    (workspace / "escape").symlink_to(escaped_target, target_is_directory=True)
    symlink_escape = client.post(
        "/read_file",
        json={"path": "escape/secret.txt"},
    )
    _assert_invalid_path(symlink_escape, "symlink_escape")

    multipart_escape = client.post(
        "/upload",
        data={"file_path": "../outside.bin"},
        files={"file": ("outside.bin", b"x", "application/octet-stream")},
    )
    _assert_invalid_path(multipart_escape, "path_traversal")


@pytest.mark.asyncio
async def test_shell_cwd_uses_the_same_path_policy(
    filesystem_contract: FilesystemContract,
) -> None:
    """A rejected cwd fails before Ship attempts to execute the command."""
    with pytest.raises(HTTPException) as exc_info:
        await run_command("echo should-not-run", cwd="../outside")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "invalid_path"
    assert exc_info.value.detail["details"]["reason"] == "path_traversal"
