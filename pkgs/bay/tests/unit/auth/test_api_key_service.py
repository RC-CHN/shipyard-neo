"""Unit tests for ApiKeyService.

Tests key generation, hashing, verification, auto-provisioning,
and credentials file output.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.api_key import ApiKeyService


class TestGenerateKey:
    """Test API key generation."""

    def test_format(self):
        """Generated key has sk-bay- prefix and correct length."""
        plaintext, key_hash, key_prefix = ApiKeyService.generate_key()

        assert plaintext.startswith("sk-bay-")
        # sk-bay- (7 chars) + 64 hex chars = 71 total
        assert len(plaintext) == 71

    def test_hash_is_sha256(self):
        """Hash is a valid SHA-256 hex digest."""
        plaintext, key_hash, key_prefix = ApiKeyService.generate_key()

        expected = hashlib.sha256(plaintext.encode()).hexdigest()
        assert key_hash == expected
        assert len(key_hash) == 64

    def test_prefix_is_first_12_chars(self):
        """Key prefix is the first 12 characters of plaintext."""
        plaintext, key_hash, key_prefix = ApiKeyService.generate_key()

        assert key_prefix == plaintext[:12]

    def test_uniqueness(self):
        """Each call generates a unique key."""
        keys = {ApiKeyService.generate_key()[0] for _ in range(10)}
        assert len(keys) == 10


class TestHashAndVerify:
    """Test key hashing and verification."""

    def test_hash_deterministic(self):
        """Same input produces same hash."""
        h1 = ApiKeyService.hash_key("test-key")
        h2 = ApiKeyService.hash_key("test-key")
        assert h1 == h2

    def test_hash_different_inputs(self):
        """Different inputs produce different hashes."""
        h1 = ApiKeyService.hash_key("key-a")
        h2 = ApiKeyService.hash_key("key-b")
        assert h1 != h2

    def test_verify_correct(self):
        """verify_key returns True for correct key."""
        plaintext = "sk-bay-test123"
        key_hash = ApiKeyService.hash_key(plaintext)
        assert ApiKeyService.verify_key(plaintext, key_hash) is True

    def test_verify_incorrect(self):
        """verify_key returns False for wrong key."""
        key_hash = ApiKeyService.hash_key("correct-key")
        assert ApiKeyService.verify_key("wrong-key", key_hash) is False


class TestWriteCredentialsFile:
    """Test credentials file output."""

    def test_writes_json(self, tmp_path: Path):
        """Credentials file contains expected JSON fields."""
        ApiKeyService.write_credentials_file(
            tmp_path, "sk-bay-test", "http://localhost:8114"
        )

        cred_path = tmp_path / "credentials.json"
        assert cred_path.exists()

        data = json.loads(cred_path.read_text())
        assert data["api_key"] == "sk-bay-test"
        assert data["endpoint"] == "http://localhost:8114"
        assert "generated_at" in data

    def test_file_permissions(self, tmp_path: Path):
        """Credentials file has 0600 permissions."""
        ApiKeyService.write_credentials_file(
            tmp_path, "sk-bay-test", "http://localhost:8114"
        )

        cred_path = tmp_path / "credentials.json"
        mode = oct(cred_path.stat().st_mode)[-3:]
        assert mode == "600"

    def test_creates_parent_dirs(self, tmp_path: Path):
        """Creates parent directories if they don't exist."""
        nested = tmp_path / "sub" / "dir"
        ApiKeyService.write_credentials_file(
            nested, "sk-bay-test", "http://localhost:8114"
        )

        assert (nested / "credentials.json").exists()


class TestAutoProvision:
    """Test API key auto-provisioning."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings with security config."""
        from app.config import SecurityConfig, ServerConfig

        settings = MagicMock()
        settings.security = SecurityConfig(api_key=None, allow_anonymous=True)
        settings.server = ServerConfig(host="0.0.0.0", port=8114)
        return settings

    @pytest.fixture
    def mock_db(self):
        """Create mock async database session."""
        db = MagicMock()
        # Default: no existing keys
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.scalars.return_value.first.return_value = None
        db.execute = AsyncMock(return_value=result)
        db.flush = AsyncMock()
        db.add = MagicMock()
        return db

    @pytest.mark.asyncio
    async def test_first_boot_generates_key(self, mock_db, mock_settings, tmp_path):
        """First boot with no keys generates a new key and writes credentials."""
        with patch.dict(os.environ, {"BAY_DATA_DIR": str(tmp_path)}, clear=False):
            # Remove BAY_API_KEY if present
            env = {k: v for k, v in os.environ.items() if k != "BAY_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                result = await ApiKeyService.auto_provision(mock_db, mock_settings)

        # Should have added a key to DB
        mock_db.add.assert_called_once()
        added_key = mock_db.add.call_args[0][0]
        assert added_key.owner == "default"
        assert added_key.is_active is True
        assert added_key.key_prefix.startswith("sk-bay-")

        # Should return hash→owner mapping
        assert len(result) == 1
        assert list(result.values()) == ["default"]

        # Should have written credentials file
        cred_path = tmp_path / "credentials.json"
        assert cred_path.exists()
        data = json.loads(cred_path.read_text())
        assert data["api_key"].startswith("sk-bay-")

    @pytest.mark.asyncio
    async def test_existing_keys_skip_generation(self, mock_db, mock_settings):
        """Existing keys in DB prevent generation."""
        from app.models.api_key import ApiKey

        existing = ApiKey(
            id="existing",
            key_hash="abc123",
            key_prefix="sk-bay-exist",
            owner="default",
            is_active=True,
        )

        # First call: check env var (no BAY_API_KEY)
        # Second call: check existing keys — returns existing
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                # First query: check existing active keys
                result.scalars.return_value.first.return_value = existing
                result.scalars.return_value.all.return_value = [existing]
            else:
                # Second query: load_active_key_hashes
                result.scalars.return_value.all.return_value = [existing]
            return result

        mock_db.execute.side_effect = side_effect

        with patch.dict(os.environ, {}, clear=False):
            env = {k: v for k, v in os.environ.items() if k != "BAY_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                result = await ApiKeyService.auto_provision(mock_db, mock_settings)

        mock_db.add.assert_not_called()
        assert result == {"abc123": "default"}

    @pytest.mark.asyncio
    async def test_env_var_seeds_key(self, mock_db, mock_settings):
        """BAY_API_KEY env var seeds key to DB."""
        # First execute: check if already seeded → no
        # Second execute: load_active_key_hashes
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value.first.return_value = None
            else:
                from app.models.api_key import ApiKey

                seeded = ApiKey(
                    id="seeded",
                    key_hash=ApiKeyService.hash_key("sk-bay-from-env"),
                    key_prefix="sk-bay-from",
                    owner="default",
                )
                result.scalars.return_value.all.return_value = [seeded]
            return result

        mock_db.execute.side_effect = side_effect

        with patch.dict(os.environ, {"BAY_API_KEY": "sk-bay-from-env"}, clear=False):
            await ApiKeyService.auto_provision(mock_db, mock_settings)

        mock_db.add.assert_called_once()
        added = mock_db.add.call_args[0][0]
        assert added.key_hash == ApiKeyService.hash_key("sk-bay-from-env")


class TestAuthenticateWithDbKey:
    """Test authenticate() with DB-stored key hashes."""

    def _create_mock_request(
        self,
        headers: dict[str, str] | None = None,
        api_key_hashes: dict[str, str] | None = None,
    ):
        """Create mock request with optional app.state.api_key_hashes."""
        from fastapi import Request

        request = MagicMock(spec=Request)
        request.headers = headers or {}
        request.app.state.api_key_hashes = api_key_hashes or {}
        return request

    def _create_mock_settings(
        self,
        allow_anonymous: bool = True,
    ):
        from app.config import SecurityConfig, Settings

        settings = MagicMock(spec=Settings)
        settings.security = SecurityConfig(
            allow_anonymous=allow_anonymous
        )
        return settings

    def test_db_key_matches(self):
        """Valid DB key returns correct owner."""
        from app.api.dependencies import authenticate

        plaintext = "sk-bay-testkey123"
        key_hash = ApiKeyService.hash_key(plaintext)
        hashes = {key_hash: "team-alpha"}

        request = self._create_mock_request(
            headers={"Authorization": f"Bearer {plaintext}"},
            api_key_hashes=hashes,
        )
        settings = self._create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "team-alpha"

    def test_db_key_no_match_raises_401(self):
        """Invalid key against DB hashes raises UnauthorizedError."""
        from app.api.dependencies import authenticate
        from app.errors import UnauthorizedError

        hashes = {ApiKeyService.hash_key("correct-key"): "default"}

        request = self._create_mock_request(
            headers={"Authorization": "Bearer wrong-key"},
            api_key_hashes=hashes,
        )
        settings = self._create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError):
                authenticate(request)

    def test_empty_hashes_fallback_anonymous(self):
        """No DB keys + anonymous mode → allow."""
        from app.api.dependencies import authenticate

        request = self._create_mock_request(
            headers={"Authorization": "Bearer any-token"},
            api_key_hashes={},
        )
        settings = self._create_mock_settings(allow_anonymous=True)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            result = authenticate(request)

        assert result == "default"

    def test_empty_hashes_strict_raises_401(self):
        """No DB keys + strict mode → 401."""
        from app.api.dependencies import authenticate
        from app.errors import UnauthorizedError

        request = self._create_mock_request(
            headers={"Authorization": "Bearer any-token"},
            api_key_hashes={},
        )
        settings = self._create_mock_settings(allow_anonymous=False)

        with patch("app.api.dependencies.get_settings", return_value=settings):
            with pytest.raises(UnauthorizedError):
                authenticate(request)


