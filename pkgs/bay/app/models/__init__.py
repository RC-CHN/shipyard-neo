"""SQLModel data models."""

from app.models.idempotency import IdempotencyKey
from app.models.cargo import Cargo
from app.models.session import Session, SessionStatus
from app.models.sandbox import Sandbox

# Rebuild models to resolve forward references
# This is required because models use `from __future__ import annotations`
# and TYPE_CHECKING imports for circular dependency resolution
Cargo.model_rebuild()
Session.model_rebuild()
Sandbox.model_rebuild()

__all__ = [
    "IdempotencyKey",
    "Sandbox",
    "Session",
    "SessionStatus",
    "Cargo",
]
