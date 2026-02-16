"""Idempotency key data model.

Used to ensure POST /v1/sandboxes is idempotent.
See: plans/bay-api.md section 1.2
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.utils.datetime import utcnow


class IdempotencyKey(SQLModel, table=True):
    """Idempotency key for POST operations."""

    __tablename__ = "idempotency_keys"

    # Composite primary key: owner + key
    owner: str = Field(primary_key=True)
    key: str = Field(primary_key=True)

    # Request fingerprint (path + method + body hash)
    request_fingerprint: str = Field(default="")

    # Cached response
    response_snapshot: str = Field(default="")  # JSON string
    status_code: int = Field(default=200)

    # TTL
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime = Field(index=True)

    def is_expired(self) -> bool:
        """Check if this idempotency key has expired."""
        return utcnow() > self.expires_at
