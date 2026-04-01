"""Learning and skill lifecycle models.

These models provide control-plane primitives for:
- Execution evidence persistence
- Skill candidate lifecycle
- Evaluation records
- Release promotion and rollback
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlmodel import Field, SQLModel

from app.utils.datetime import utcnow


class ExecutionType(str, Enum):
    """Execution type for evidence records."""

    PYTHON = "python"
    SHELL = "shell"
    BROWSER = "browser"
    BROWSER_BATCH = "browser_batch"


class SkillCandidateStatus(str, Enum):
    """Candidate lifecycle states."""

    DRAFT = "draft"
    EVALUATING = "evaluating"
    PROMOTED = "promoted"
    PROMOTED_CANARY = "promoted_canary"
    PROMOTED_STABLE = "promoted_stable"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class SkillReleaseStage(str, Enum):
    """Release stages for promoted skills."""

    CANARY = "canary"
    STABLE = "stable"


class SkillType(str, Enum):
    """Skill classification."""

    CODE = "code"
    BROWSER = "browser"


class SkillReleaseMode(str, Enum):
    """How a release was promoted."""

    MANUAL = "manual"
    AUTO = "auto"


class LearnStatus(str, Enum):
    """Learning pipeline status for an execution."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    SKIPPED = "skipped"
    ERROR = "error"


class ExecutionHistory(SQLModel, table=True):
    """Execution evidence for learning workflows."""

    __tablename__ = "execution_history"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)
    sandbox_id: str = Field(index=True)
    session_id: str | None = Field(default=None, index=True)

    exec_type: ExecutionType
    code: str
    success: bool
    execution_time_ms: int

    output: str | None = Field(default=None)
    error: str | None = Field(default=None)
    payload_ref: str | None = Field(default=None, index=True)

    # Learning metadata (agent annotations)
    description: str | None = Field(default=None)
    tags: str | None = Field(default=None)
    notes: str | None = Field(default=None)
    learn_enabled: bool = Field(default=False, index=True)
    learn_status: LearnStatus | None = Field(default=None, index=True)
    learn_error: str | None = Field(default=None)
    learn_processed_at: datetime | None = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=utcnow, index=True)


class ArtifactBlob(SQLModel, table=True):
    """Externalized payload storage for larger JSON evidence."""

    __tablename__ = "artifact_blobs"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)
    kind: str = Field(default="generic", index=True)
    payload_json: str
    created_at: datetime = Field(default_factory=utcnow, index=True)


class SkillCandidate(SQLModel, table=True):
    """Candidate skill proposed by an agent/client."""

    __tablename__ = "skill_candidates"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    skill_key: str = Field(index=True)
    scenario_key: str | None = Field(default=None, index=True)
    payload_ref: str | None = Field(default=None)
    payload_hash: str | None = Field(default=None, index=True)
    skill_type: SkillType = Field(default=SkillType.CODE, index=True)
    auto_release_eligible: bool = Field(default=False, index=True)
    auto_release_reason: str | None = Field(default=None)

    # Human-readable skill documentation fields.
    summary: str | None = Field(default=None)
    usage_notes: str | None = Field(default=None)
    preconditions_json: str | None = Field(default=None)
    postconditions_json: str | None = Field(default=None)

    # Comma-separated execution IDs used as source evidence.
    source_execution_ids: str = Field(default="")

    status: SkillCandidateStatus = Field(default=SkillCandidateStatus.DRAFT, index=True)
    is_deleted: bool = Field(default=False, index=True)

    created_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)

    latest_score: float | None = Field(default=None)
    latest_pass: bool | None = Field(default=None)
    last_evaluated_at: datetime | None = Field(default=None)

    promotion_release_id: str | None = Field(default=None, index=True)
    # Evolution lineage (Phase 2)
    evolution_parent_id: str | None = Field(default=None, index=True)
    evolution_meta_prompt_id: str | None = Field(default=None, index=True)
    mutation_reasoning: str | None = Field(default=None)
    deleted_at: datetime | None = Field(default=None, index=True)
    deleted_by: str | None = Field(default=None)
    delete_reason: str | None = Field(default=None)


class SkillEvaluation(SQLModel, table=True):
    """Evaluation records for a candidate skill."""

    __tablename__ = "skill_evaluations"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    candidate_id: str = Field(foreign_key="skill_candidates.id", index=True)
    benchmark_id: str | None = Field(default=None, index=True)

    score: float | None = Field(default=None)
    passed: bool = Field(index=True)
    report: str | None = Field(default=None)

    evaluated_by: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class MetaPrompt(SQLModel, table=True):
    """Mutation instruction in the MetaPrompt archive.

    Each record is a strategy for improving a skill (e.g., 'add error handling').
    Selection weight is derived from success_count / (usage_count + 1).
    """

    __tablename__ = "meta_prompts"

    id: str = Field(primary_key=True)
    instruction: str
    is_default: bool = Field(default=False, index=True)

    usage_count: int = Field(default=0)
    success_count: int = Field(default=0)

    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)


class SkillGoal(SQLModel, table=True):
    """Declared goal for a skill key — the human's intent input."""

    __tablename__ = "skill_goals"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    skill_key: str = Field(index=True)

    goal: str
    rubric_summary: str = Field(default="")
    rubric_json: str | None = Field(default=None)  # JSON: SkillRubric structure

    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)


class SkillOutcome(SQLModel, table=True):
    """Execution outcome report — the evolution signal feed."""

    __tablename__ = "skill_outcomes"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    skill_key: str = Field(index=True)
    release_id: str = Field(index=True)

    outcome: str = Field(index=True)  # "success" | "failure" | "partial"
    reasoning: str
    execution_id: str | None = Field(default=None, index=True)
    signals_json: str | None = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow, index=True)


class SkillRelease(SQLModel, table=True):
    """Promoted, versioned skill release."""

    __tablename__ = "skill_releases"

    id: str = Field(primary_key=True)
    owner: str = Field(index=True)

    skill_key: str = Field(index=True)
    candidate_id: str = Field(foreign_key="skill_candidates.id", index=True)

    version: int = Field(index=True)
    stage: SkillReleaseStage = Field(default=SkillReleaseStage.CANARY, index=True)
    is_active: bool = Field(default=True, index=True)
    is_deleted: bool = Field(default=False, index=True)
    release_mode: SkillReleaseMode = Field(default=SkillReleaseMode.MANUAL, index=True)

    promoted_by: str | None = Field(default=None)
    promoted_at: datetime = Field(default_factory=utcnow, index=True)

    rollback_of: str | None = Field(default=None, index=True)
    auto_promoted_from: str | None = Field(default=None, index=True)
    health_window_end_at: datetime | None = Field(default=None, index=True)

    # Human-readable release-upgrade metadata.
    upgrade_of_release_id: str | None = Field(default=None, index=True)
    upgrade_reason: str | None = Field(default=None)
    change_summary: str | None = Field(default=None)

    deleted_at: datetime | None = Field(default=None, index=True)
    deleted_by: str | None = Field(default=None)
    delete_reason: str | None = Field(default=None)
