"""Idempotency service for ensuring idempotent POST operations.

Implements Idempotency-Key header handling for POST /v1/sandboxes.
See: plans/phase-1/idempotency-design.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.errors import ConflictError
from app.models.idempotency import IdempotencyKey

if TYPE_CHECKING:
    from app.config import IdempotencyConfig

logger = logging.getLogger(__name__)

# Key format validation: alphanumeric, dash, underscore, max 128 chars
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


@dataclass
class CachedResponse:
    """Cached idempotency response."""

    response: dict[str, Any]
    status_code: int


class IdempotencyService:
    """Service for handling idempotency keys.

    Ensures that POST /v1/sandboxes with the same Idempotency-Key returns
    the same response, preventing duplicate resource creation on retries.

    Flow:
    1. check() - Check if key exists and fingerprint matches
    2. save() - Save key + fingerprint + response after successful creation
    """

    def __init__(
        self,
        db_session: AsyncSession,
        config: IdempotencyConfig | None = None,
    ):
        self.db_session = db_session
        # config is IdempotencyConfig from app.config (Pydantic model)
        # If None, use defaults directly
        self._enabled = config.enabled if config else True
        self._ttl_hours = config.ttl_hours if config else 1

    @property
    def enabled(self) -> bool:
        """Whether idempotency is enabled."""
        return self._enabled

    @property
    def ttl_hours(self) -> int:
        """TTL in hours for idempotency keys."""
        return self._ttl_hours

    @staticmethod
    def validate_key(key: str) -> bool:
        """Validate idempotency key format.

        Args:
            key: The idempotency key to validate

        Returns:
            True if valid, False otherwise

        Key format:
        - 1-128 characters
        - Alphanumeric, dash, underscore only
        """
        return bool(IDEMPOTENCY_KEY_PATTERN.match(key))

    @staticmethod
    def compute_fingerprint(path: str, method: str, body: str) -> str:
        """Compute request fingerprint for conflict detection.

        Args:
            path: Request path (e.g., "/v1/sandboxes")
            method: HTTP method (e.g., "POST")
            body: Request body as JSON string

        Returns:
            SHA256 hash of path + method + body
        """
        content = f"{method}:{path}:{body}"
        return hashlib.sha256(content.encode()).hexdigest()

    async def check(
        self,
        owner: str,
        key: str,
        path: str,
        method: str,
        body: str,
    ) -> CachedResponse | None:
        """Check if idempotency key exists and validate fingerprint.

        Args:
            owner: Owner ID (namespace for the key)
            key: Idempotency-Key header value
            path: Request path
            method: HTTP method
            body: Request body as JSON string

        Returns:
            CachedResponse if key exists and is valid, None otherwise

        Raises:
            ConflictError: If key exists but fingerprint doesn't match
        """
        if not self.enabled:
            return None

        # Validate key format
        if not self.validate_key(key):
            raise ConflictError(
                message=f"Invalid Idempotency-Key format: must be 1-128 alphanumeric characters, dash, or underscore",
                details={"key": key},
            )

        # Query existing record
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.owner == owner,
            IdempotencyKey.key == key,
        )
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            return None

        # Check expiration - lazy cleanup
        if record.is_expired():
            logger.debug(
                "Idempotency key expired, deleting",
                extra={"owner": owner, "key": key},
            )
            await self.db_session.delete(record)
            await self.db_session.flush()
            return None

        # Compute current fingerprint
        fingerprint = self.compute_fingerprint(path, method, body)

        # Check fingerprint match
        if record.request_fingerprint != fingerprint:
            logger.warning(
                "Idempotency key conflict: fingerprint mismatch",
                extra={
                    "owner": owner,
                    "key": key,
                    "stored_fingerprint": record.request_fingerprint[:16] + "...",
                    "request_fingerprint": fingerprint[:16] + "...",
                },
            )
            raise ConflictError(
                message="Idempotency key already used with different request parameters",
                details={
                    "key": key,
                    "hint": "Use a different Idempotency-Key for different request parameters",
                },
            )

        # Return cached response
        logger.info(
            "Returning cached idempotency response",
            extra={"owner": owner, "key": key},
        )
        return CachedResponse(
            response=json.loads(record.response_snapshot),
            status_code=record.status_code,
        )

    async def save(
        self,
        owner: str,
        key: str,
        path: str,
        method: str,
        body: str,
        response: Any,
        status_code: int,
    ) -> None:
        """Save idempotency key with response.

        Args:
            owner: Owner ID
            key: Idempotency-Key header value
            path: Request path
            method: HTTP method
            body: Request body as JSON string
            response: Response object (Pydantic model or dict)
            status_code: HTTP status code
        """
        if not self.enabled:
            return

        fingerprint = self.compute_fingerprint(path, method, body)

        # Serialize response
        #
        # IMPORTANT: for Pydantic models, use model_dump_json() so datetime fields
        # are encoded in the same ISO-8601 format FastAPI uses in normal responses.
        # Otherwise, default=str would produce "YYYY-MM-DD HH:MM:SS" which breaks
        # idempotency replay byte-for-byte comparisons.
        if hasattr(response, "model_dump_json"):
            response_json = response.model_dump_json()
        elif hasattr(response, "model_dump"):
            response_json = json.dumps(response.model_dump(), default=str)
        elif isinstance(response, dict):
            response_json = json.dumps(response, default=str)
        else:
            response_json = json.dumps(response, default=str)

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=self.ttl_hours)

        record = IdempotencyKey(
            owner=owner,
            key=key,
            request_fingerprint=fingerprint,
            response_snapshot=response_json,
            status_code=status_code,
            created_at=now,
            expires_at=expires_at,
        )

        try:
            self.db_session.add(record)
            await self.db_session.flush()
            logger.info(
                "Saved idempotency key",
                extra={
                    "owner": owner,
                    "key": key,
                    "expires_at": expires_at.isoformat(),
                },
            )
        except Exception as e:
            # Handle race condition: another request saved the same key
            # This is fine - the first one wins
            logger.warning(
                "Failed to save idempotency key (likely race condition)",
                extra={"owner": owner, "key": key, "error": str(e)},
            )
            await self.db_session.rollback()

    async def cleanup_expired(self, batch_size: int = 100) -> int:
        """Cleanup expired idempotency keys.

        This is an optional batch cleanup method. The primary cleanup
        happens lazily during check().

        Args:
            batch_size: Maximum number of records to delete per call

        Returns:
            Number of deleted records
        """
        now = datetime.utcnow()
        stmt = (
            delete(IdempotencyKey)
            .where(IdempotencyKey.expires_at < now)
            .execution_options(synchronize_session=False)
        )

        # Note: SQLite doesn't support LIMIT in DELETE
        # For production with PostgreSQL, add .limit(batch_size)
        result = await self.db_session.execute(stmt)
        deleted = result.rowcount
        await self.db_session.flush()

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired idempotency keys")

        return deleted
