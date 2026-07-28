"""
Unit tests for workspace module.
"""

import json

import pytest
from unittest.mock import patch
from fastapi import HTTPException


# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


class TestResolvePathUnit:
    """Test resolve_path function in isolation (mocking WORKSPACE_ROOT)"""

    def test_resolve_relative_path(self, tmp_path):
        """Test resolving a relative path within workspace"""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            # Create a test file
            (tmp_path / "test.txt").touch()

            result = resolve_path("test.txt")
            assert result == tmp_path / "test.txt"

    def test_resolve_nested_path(self, tmp_path):
        """Test resolving nested relative path"""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            # Create nested directory
            (tmp_path / "subdir").mkdir()
            (tmp_path / "subdir" / "file.txt").touch()

            result = resolve_path("subdir/file.txt")
            assert result == tmp_path / "subdir" / "file.txt"

    def test_resolve_absolute_path_within_workspace(self, tmp_path):
        """Test resolving absolute path that is within workspace"""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            (tmp_path / "test.txt").touch()

            result = resolve_path(str(tmp_path / "test.txt"))
            assert result == tmp_path / "test.txt"

    def test_reject_path_outside_workspace(self, tmp_path):
        """Test that paths outside workspace are rejected"""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            with pytest.raises(HTTPException) as exc_info:
                resolve_path("/etc/passwd")

            assert exc_info.value.status_code == 403
            assert exc_info.value.detail["code"] == "invalid_path"
            assert exc_info.value.detail["details"]["reason"] == "outside_allowed_roots"

    def test_reject_path_traversal(self, tmp_path):
        """Test that path traversal attacks are rejected"""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            with pytest.raises(HTTPException) as exc_info:
                resolve_path("../../../etc/passwd")

            assert exc_info.value.status_code == 403

    def test_resolve_dot_path(self, tmp_path):
        """Test resolving current directory path"""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            result = resolve_path(".")
            assert result == tmp_path

    def test_allows_tmp_absolute_path_by_default(self, tmp_path):
        """The standalone Ship policy permits an absolute /tmp path."""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            target = tmp_path.parent / "ship-tmp-file.txt"
            assert resolve_path(str(target)) == target

    def test_normalizes_absolute_path(self, tmp_path):
        """Absolute paths are lexically normalized before their root check."""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            target = tmp_path / "nested" / "file.txt"
            result = resolve_path(f"{tmp_path}/nested/../nested/./file.txt")
            assert result == target

    def test_rejects_similar_root_prefix(self, tmp_path, monkeypatch):
        """A string prefix such as workspace-evil must not match workspace."""
        monkeypatch.setenv("BAY_FILESYSTEM_ALLOWED_ROOTS_JSON", json.dumps([str(tmp_path)]))
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            with pytest.raises(HTTPException) as exc_info:
                resolve_path(f"{tmp_path}-evil/file.txt")

            assert exc_info.value.detail["details"]["reason"] == "outside_allowed_roots"

    def test_rejects_control_character(self, tmp_path):
        """Control characters cannot be used to smuggle a path through APIs."""
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import resolve_path

            with pytest.raises(HTTPException) as exc_info:
                resolve_path("safe\nname.txt")

            assert exc_info.value.detail["details"]["reason"] == "control_character"

    def test_rejects_symlink_escape(self, tmp_path, monkeypatch):
        """Resolved paths cannot escape an allowed root through a symlink."""
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "escape").symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv(
            "BAY_FILESYSTEM_ALLOWED_ROOTS_JSON", json.dumps([str(workspace)])
        )

        with patch("app.workspace.WORKSPACE_ROOT", workspace):
            from app.workspace import resolve_path

            with pytest.raises(HTTPException) as exc_info:
                resolve_path("escape/secret.txt")

            assert exc_info.value.detail["details"]["reason"] == "symlink_escape"


class TestShellCwdPolicy:
    """Shell working directories use the same resolver as filesystem APIs."""

    @pytest.mark.asyncio
    async def test_rejects_cwd_outside_allowed_roots(self, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv(
            "BAY_FILESYSTEM_ALLOWED_ROOTS_JSON", json.dumps([str(workspace)])
        )

        with patch("app.workspace.WORKSPACE_ROOT", workspace):
            from app.components.user_manager import run_command

            with pytest.raises(HTTPException) as exc_info:
                await run_command("pwd", cwd="../outside")

            assert exc_info.value.detail["code"] == "invalid_path"


class TestGetWorkspaceDir:
    """Test get_workspace_dir function"""

    def test_creates_workspace_if_not_exists(self, tmp_path):
        """Test that workspace directory is created if it doesn't exist"""
        workspace = tmp_path / "workspace"
        with patch("app.workspace.WORKSPACE_ROOT", workspace):
            from app.workspace import get_workspace_dir

            result = get_workspace_dir()
            assert result == workspace
            assert workspace.exists()

    def test_returns_existing_workspace(self, tmp_path):
        """Test that workspace directory is returned when it already exists."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch("app.workspace.WORKSPACE_ROOT", workspace):
            from app.workspace import get_workspace_dir

            result = get_workspace_dir()
            assert result == workspace


class TestPathPolicyMetadata:
    """Ship advertises the same active roots it enforces."""

    def test_policy_declares_absolute_paths_and_allowed_roots(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "BAY_FILESYSTEM_ALLOWED_ROOTS_JSON", json.dumps([str(tmp_path), "/tmp"])
        )
        with patch("app.workspace.WORKSPACE_ROOT", tmp_path):
            from app.workspace import get_path_policy

            assert get_path_policy() == {
                "accepts_absolute_paths": True,
                "allowed_roots": [str(tmp_path), "/tmp"],
            }


class TestDownloadHeaders:
    """Attachment headers are safe for non-Latin-1 filenames."""

    def test_content_disposition_has_ascii_fallback_and_rfc5987_name(self):
        from app.components.filesystem import _content_disposition

        header = _content_disposition("报告 emoji 🧪.txt")

        header.encode("ascii")
        assert 'filename="__ emoji _.txt"' in header
        assert "filename*=UTF-8''%E6%8A%A5%E5%91%8A%20emoji%20%F0%9F%A7%AA.txt" in header
