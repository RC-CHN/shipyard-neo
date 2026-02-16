"""API Key data model.

Stores hashed API keys for authentication.
Plaintext keys are never stored — only SHA-256 hashes.
"""

from datetime import UTC, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ApiKey(SQLModel, table=True):
    """API Key for Bay authentication.

    Keys are stored as SHA-256 hashes. The key_prefix (first 12 chars)
    is stored for identification in logs and admin UIs.
    """

    __tablename__ = "api_keys"

    id: str = Field(primary_key=True)
    key_hash: str = Field(index=True)  # SHA-256 hex digest
    key_prefix: str = Field()  # First 12 chars of plaintext (e.g., "sk-bay-abc1")
    owner: str = Field(default="default")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deactivated_at: Optional[datetime] = Field(default=None)
