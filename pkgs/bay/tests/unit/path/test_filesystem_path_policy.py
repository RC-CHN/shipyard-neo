"""Tests for the negotiated filesystem path policy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from app.adapters.base import RuntimePathPolicy
from app.adapters.ship import _parse_path_policy, _raise_ship_response_error
from app.api.v1.capabilities import _content_disposition
from app.config import ContainerSpec, FilesystemConfig, Settings
from app.drivers.docker.docker import DockerDriver
from app.drivers.k8s.k8s import K8sDriver
from app.errors import CapabilityNotSupportedError, InvalidPathError
from app.validators.path import normalize_sandbox_path, path_for_runtime


class TestFilesystemConfig:
    """The Bay configuration validates and serializes one shared policy."""

    def test_defaults_match_the_public_contract(self) -> None:
        config = FilesystemConfig()

        assert config.allowed_roots == ["/workspace", "/tmp"]
        assert config.allowed_roots_json() == '["/workspace","/tmp"]'

    @pytest.mark.parametrize(
        "roots",
        [
            ["workspace", "/tmp"],
            ["/tmp"],
            ["/workspace", "/workspace/../../etc"],
            ["/workspace", "/tmp\x00unsafe"],
        ],
    )
    def test_rejects_noncanonical_or_unsafe_roots(self, roots: list[str]) -> None:
        with pytest.raises(PydanticValidationError):
            FilesystemConfig(allowed_roots=roots)

    def test_canonicalizes_and_deduplicates_roots(self) -> None:
        config = FilesystemConfig(allowed_roots=["/workspace/./", "/tmp/", "/tmp"])

        assert config.allowed_roots == ["/workspace", "/tmp"]


class TestPublicPathNormalization:
    """Every request shape uses this same lexical normalization contract."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("file.txt", "/workspace/file.txt"),
            ("nested/../file.txt", "/workspace/file.txt"),
            ("/workspace/./nested/../file.txt", "/workspace/file.txt"),
            ("/tmp/./artifact.bin", "/tmp/artifact.bin"),
        ],
    )
    def test_normalizes_relative_and_absolute_paths(self, path: str, expected: str) -> None:
        assert (
            normalize_sandbox_path(
                path,
                allowed_roots=["/workspace", "/tmp"],
            )
            == expected
        )

    @pytest.mark.parametrize(
        "path,reason",
        [
            ("../secret.txt", "path_traversal"),
            ("/workspace-evil/file.txt", "outside_allowed_roots"),
            ("/etc/passwd", "outside_allowed_roots"),
            ("safe\r\nname.txt", "control_character"),
            ("C:\\temp\\file.txt", "windows_path"),
        ],
    )
    def test_rejects_unsafe_paths(self, path: str, reason: str) -> None:
        with pytest.raises(InvalidPathError) as exc_info:
            normalize_sandbox_path(path, allowed_roots=["/workspace", "/tmp"])

        assert exc_info.value.details["reason"] == reason
        assert exc_info.value.details["allowed_roots"] == ["/workspace", "/tmp"]


class TestRuntimePathCompatibility:
    """Bay forwards absolute paths only to Ships that advertise support."""

    def test_new_ship_receives_normalized_absolute_path(self) -> None:
        assert (
            path_for_runtime(
                "/tmp/result.bin",
                accepts_absolute_paths=True,
                allowed_roots=["/workspace", "/tmp"],
            )
            == "/tmp/result.bin"
        )

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/workspace", "."),
            ("/workspace/project/file.txt", "project/file.txt"),
        ],
    )
    def test_old_ship_receives_workspace_relative_fallback(self, path: str, expected: str) -> None:
        assert (
            path_for_runtime(
                path,
                accepts_absolute_paths=False,
                allowed_roots=["/workspace"],
            )
            == expected
        )

    def test_old_ship_rejects_tmp_without_guessing(self) -> None:
        with pytest.raises(CapabilityNotSupportedError) as exc_info:
            path_for_runtime(
                "/tmp/result.bin",
                accepts_absolute_paths=False,
                allowed_roots=["/workspace"],
            )

        assert exc_info.value.code == "capability_not_supported"
        assert exc_info.value.details["allowed_roots"] == ["/workspace"]

    def test_meta_parser_keeps_missing_policy_legacy(self) -> None:
        assert _parse_path_policy({"mount_path": "/workspace"}) is None

    def test_meta_parser_reads_new_ship_policy(self) -> None:
        policy = _parse_path_policy(
            {
                "path_policy": {
                    "accepts_absolute_paths": True,
                    "allowed_roots": ["/workspace", "/tmp"],
                }
            }
        )

        assert policy == RuntimePathPolicy(True, ("/workspace", "/tmp"))


class TestShipErrorTranslation:
    """Structured Ship rejections survive Bay without becoming ship_error."""

    def test_invalid_path_details_are_preserved(self) -> None:
        response = httpx.Response(
            403,
            json={
                "detail": {
                    "code": "invalid_path",
                    "message": "path is outside the allowed roots",
                    "details": {
                        "reason": "symlink_escape",
                        "allowed_roots": ["/workspace", "/tmp"],
                    },
                }
            },
        )

        with pytest.raises(InvalidPathError) as exc_info:
            _raise_ship_response_error(response, operation="Download")

        assert exc_info.value.code == "invalid_path"
        assert exc_info.value.details == {
            "reason": "symlink_escape",
            "allowed_roots": ["/workspace", "/tmp"],
        }


class TestDriverPolicyInjection:
    """Docker and Kubernetes hand the identical JSON policy to Ship."""

    @staticmethod
    def _settings() -> Settings:
        return Settings(filesystem={"allowed_roots": ["/workspace", "/tmp"]})

    @staticmethod
    def _session() -> SimpleNamespace:
        return SimpleNamespace(id="session-1", sandbox_id="sandbox-1", profile_id="default")

    @staticmethod
    def _cargo() -> SimpleNamespace:
        return SimpleNamespace(id="cargo-1", driver_ref="cargo-volume")

    def test_docker_injects_policy_for_ship_only(self) -> None:
        driver = DockerDriver.__new__(DockerDriver)
        driver._publish_ports = False
        driver._connect_mode = "container_network"
        spec = ContainerSpec(name="ship", image="ship:latest", runtime_type="ship")

        with patch("app.drivers.docker.docker.get_settings", return_value=self._settings()):
            config, _ = driver._build_container_config(
                spec,
                session=self._session(),
                cargo=self._cargo(),
                network_name="session-network",
            )

        assert 'BAY_FILESYSTEM_ALLOWED_ROOTS_JSON=["/workspace","/tmp"]' in config["Env"]

    def test_k8s_injects_policy_for_ship_only(self) -> None:
        driver = K8sDriver.__new__(K8sDriver)
        driver._image_pull_policy = "if_not_present"
        spec = ContainerSpec(name="ship", image="ship:latest", runtime_type="ship")

        with patch("app.drivers.k8s.k8s.get_settings", return_value=self._settings()):
            container = driver._build_k8s_container(spec, session=self._session())

        environment = {item.name: item.value for item in container.env}
        assert environment["BAY_FILESYSTEM_ALLOWED_ROOTS_JSON"] == '["/workspace","/tmp"]'


class TestDownloadDisposition:
    """Bay never emits a non-ASCII header value for a downloaded filename."""

    def test_header_has_ascii_fallback_and_utf8_extended_value(self) -> None:
        header = _content_disposition("报告 emoji 🧪.txt")

        header.encode("ascii")
        assert 'filename="__ emoji _.txt"' in header
        assert "filename*=UTF-8''%E6%8A%A5%E5%91%8A%20emoji%20%F0%9F%A7%AA.txt" in header
