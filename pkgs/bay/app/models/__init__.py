"""SQLModel data models."""

from app.models.api_key import ApiKey
from app.models.cargo import Cargo
from app.models.idempotency import IdempotencyKey
from app.models.sandbox import Sandbox
from app.models.session import Session, SessionStatus
from app.models.skill import (
    ArtifactBlob,
    ExecutionHistory,
    ExecutionType,
    LearnStatus,
    SkillCandidate,
    SkillCandidateStatus,
    SkillEvaluation,
    SkillRelease,
    SkillReleaseMode,
    SkillReleaseStage,
    SkillType,
)

# Rebuild models to resolve forward references
# This is required because models use `from __future__ import annotations`
# and TYPE_CHECKING imports for circular dependency resolution
Cargo.model_rebuild()
Session.model_rebuild()
Sandbox.model_rebuild()

__all__ = [
    "ApiKey",
    "IdempotencyKey",
    "Sandbox",
    "Session",
    "SessionStatus",
    "Cargo",
    "ExecutionHistory",
    "ExecutionType",
    "LearnStatus",
    "ArtifactBlob",
    "SkillCandidate",
    "SkillCandidateStatus",
    "SkillType",
    "SkillEvaluation",
    "SkillRelease",
    "SkillReleaseStage",
    "SkillReleaseMode",
]
