"""API Key service.

Handles key generation, hashing, verification, auto-provisioning,
and credentials file output.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import Settings
from app.models.api_key import ApiKey

logger = structlog.get_logger()

# Key format: sk-bay-{64 hex chars}
_KEY_PREFIX = "sk-bay-"
_KEY_DISPLAY_LEN = 12  # chars to store as key_prefix for identification


class ApiKeyService:
    """Service for API key lifecycle management."""

    @staticmethod
    def generate_key() -> tuple[str, str, str]:
        """Generate a new API key.

        Returns:
            Tuple of (plaintext, key_hash, key_prefix)
        """
        random_part = secrets.token_hex(32)  # 64 hex chars
        plaintext = f"{_KEY_PREFIX}{random_part}"
        key_hash = ApiKeyService.hash_key(plaintext)
        key_prefix = plaintext[:_KEY_DISPLAY_LEN]
        return plaintext, key_hash, key_prefix

    @staticmethod
    def hash_key(plaintext: str) -> str:
        """Hash a plaintext key using SHA-256.

        Args:
            plaintext: The plaintext API key

        Returns:
            SHA-256 hex digest
        """
        return hashlib.sha256(plaintext.encode()).hexdigest()

    @staticmethod
    def verify_key(plaintext: str, key_hash: str) -> bool:
        """Verify a plaintext key against a stored hash.

        Args:
            plaintext: The plaintext API key to verify
            key_hash: The stored SHA-256 hash

        Returns:
            True if the key matches
        """
        return hmac.compare_digest(
            hashlib.sha256(plaintext.encode()).hexdigest(),
            key_hash,
        )

    @staticmethod
    async def load_active_key_hashes(db: AsyncSession) -> dict[str, str]:
        """Load all active key hashes from DB into memory.

        Returns:
            Dict mapping key_hash → owner
        """
        result = await db.execute(
            select(ApiKey).where(ApiKey.is_active == True)  # noqa: E712
        )
        keys = result.scalars().all()
        return {k.key_hash: k.owner for k in keys}

    @staticmethod
    async def auto_provision(
        db: AsyncSession,
        settings: Settings,
    ) -> dict[str, str]:
        """Auto-provision API key on first boot.

        Logic:
        1. Check BAY_API_KEY env var → seed to DB if set
        2. Else check security.api_key from config → seed to DB if set
        3. Else check DB for existing active keys → skip if any exist
        4. Else generate new key → store hash in DB + write credentials.json

        Args:
            db: Database session
            settings: Application settings

        Returns:
            Dict mapping key_hash → owner (for in-memory cache)
        """

        # 1. Resolve configured key source with precedence:
        #    BAY_API_KEY env var > security.api_key from config
        configured_key = os.environ.get("BAY_API_KEY")
        source = "env_var"
        if not configured_key and settings.security.api_key:
            configured_key = settings.security.api_key
            source = "config"

        # 2. If a configured key exists, seed it to DB and return hashes
        if configured_key:
            key_hash = ApiKeyService.hash_key(configured_key)
            key_prefix = configured_key[:_KEY_DISPLAY_LEN]

            # Check if already seeded
            existing = await db.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            )
            if not existing.scalars().first():
                api_key = ApiKey(
                    id=str(uuid.uuid4()),
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    owner="default",
                    is_active=True,
                )
                db.add(api_key)
                await db.flush()
                logger.info(
                    "api_key.provision.configured",
                    source=source,
                    key_prefix=key_prefix,
                    msg="Configured API key seeded to database",
                )

            return await ApiKeyService.load_active_key_hashes(db)

        # 3. Check DB for existing keys
        existing_keys = await db.execute(
            select(ApiKey).where(ApiKey.is_active == True)  # noqa: E712
        )
        if existing_keys.scalars().first():
            logger.debug("api_key.provision.skip", reason="active keys exist in DB")
            return await ApiKeyService.load_active_key_hashes(db)

        # 4. First boot — generate new key
        plaintext, key_hash, key_prefix = ApiKeyService.generate_key()

        api_key = ApiKey(
            id=str(uuid.uuid4()),
            key_hash=key_hash,
            key_prefix=key_prefix,
            owner="default",
            is_active=True,
        )
        db.add(api_key)
        await db.flush()

        logger.info(
            "api_key.provision.generated",
            key_prefix=key_prefix,
            msg="First boot: API key auto-generated. See credentials.json for the key.",
        )

        # Write credentials file
        data_dir = Path(os.environ.get("BAY_DATA_DIR", "."))
        endpoint = f"http://{settings.server.host}:{settings.server.port}"
        ApiKeyService.write_credentials_file(data_dir, plaintext, endpoint)

        return {key_hash: "default"}

    @staticmethod
    def write_credentials_file(
        data_dir: Path,
        api_key: str,
        endpoint: str,
    ) -> None:
        """Write credentials.json for companion services.

        Args:
            data_dir: Directory to write the file to
            api_key: Plaintext API key
            endpoint: Bay endpoint URL
        """
        credentials = {
            "api_key": api_key,
            "endpoint": endpoint,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        cred_path = data_dir / "credentials.json"
        data_dir.mkdir(parents=True, exist_ok=True)

        cred_path.write_text(json.dumps(credentials, indent=2) + "\n")

        # Set file permissions to 0600 (owner read/write only)
        try:
            os.chmod(cred_path, 0o600)
        except OSError:
            # Windows or restricted environments may not support chmod
            logger.warning(
                "api_key.credentials.chmod_failed",
                path=str(cred_path),
                msg="Could not set file permissions to 0600",
            )

        logger.info(
            "api_key.credentials.written",
            path=str(cred_path),
            msg="Credentials file written for companion services",
        )
