"""Type definitions for Bay SDK.

Pydantic models for request/response serialization.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SandboxStatus(str, Enum):
    """Sandbox status enum."""

    IDLE = "idle"  # No running session
    STARTING = "starting"  # Session is starting
    READY = "ready"  # Session is running and ready
    FAILED = "failed"  # Last session start failed
    EXPIRED = "expired"  # TTL expired


class SandboxInfo(BaseModel):
    """Sandbox information."""

    id: str
    status: SandboxStatus
    profile: str
    cargo_id: str
    capabilities: list[str]
    created_at: datetime
    expires_at: datetime | None
    idle_expires_at: datetime | None


class SandboxList(BaseModel):
    """Sandbox list with pagination.

    Design:
    - SDK makes a single HTTP round-trip, returning current page items and next cursor.
    - No hidden pagination state; user decides whether to continue fetching.
    - Maps 1:1 with REST API for easy debugging.

    Example:
        cursor = None
        while True:
            page = await client.list_sandboxes(limit=50, cursor=cursor)
            for sb in page.items:
                process(sb)
            if not page.next_cursor:
                break
            cursor = page.next_cursor
    """

    items: list[SandboxInfo]
    next_cursor: str | None = None


class CargoInfo(BaseModel):
    """Cargo information."""

    id: str
    managed: bool
    managed_by_sandbox_id: str | None
    backend: str
    size_limit_mb: int
    created_at: datetime
    last_accessed_at: datetime


class CargoList(BaseModel):
    """Cargo list with pagination."""

    items: list[CargoInfo]
    next_cursor: str | None = None


class FileInfo(BaseModel):
    """File/directory information."""

    name: str
    path: str
    is_dir: bool
    size: int | None = None  # None for directories
    modified_at: datetime | None = None


class PythonExecResult(BaseModel):
    """Python execution result.

    Attributes:
        success: Whether execution completed without error
        output: Combined stdout output
        error: Error message if execution failed
        data: Rich output data from IPython kernel, including:
            - execution_count: Cell execution number
            - output.text: Text output
            - output.images: List of base64-encoded images
    """

    success: bool
    output: str
    error: str | None = None
    data: dict[str, Any] | None = None
    execution_id: str | None = None
    execution_time_ms: int | None = None
    code: str | None = None


class ShellExecResult(BaseModel):
    """Shell execution result."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    execution_id: str | None = None
    execution_time_ms: int | None = None
    command: str | None = None


class BrowserExecResult(BaseModel):
    """Browser automation execution result."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None


class ProfileInfo(BaseModel):
    """Profile information."""

    id: str
    image: str
    resources: dict[str, Any]
    capabilities: list[str]
    idle_timeout: int


class ProfileList(BaseModel):
    """Profile list response."""

    items: list[ProfileInfo]


class ExecutionHistoryEntry(BaseModel):
    """Execution history entry."""

    id: str
    session_id: str | None = None
    exec_type: str
    code: str
    success: bool
    execution_time_ms: int
    output: str | None = None
    error: str | None = None
    description: str | None = None
    tags: str | None = None
    notes: str | None = None
    created_at: datetime


class ExecutionHistoryList(BaseModel):
    """Execution history list response."""

    entries: list[ExecutionHistoryEntry]
    total: int


class SkillCandidateStatus(str, Enum):
    """Skill candidate lifecycle status."""

    DRAFT = "draft"
    EVALUATING = "evaluating"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class SkillReleaseStage(str, Enum):
    """Skill release stage."""

    CANARY = "canary"
    STABLE = "stable"


class SkillCandidateInfo(BaseModel):
    """Skill candidate information."""

    id: str
    skill_key: str
    scenario_key: str | None = None
    payload_ref: str | None = None
    source_execution_ids: list[str]
    status: SkillCandidateStatus
    latest_score: float | None = None
    latest_pass: bool | None = None
    last_evaluated_at: datetime | None = None
    promotion_release_id: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class SkillCandidateList(BaseModel):
    """Skill candidate list response."""

    items: list[SkillCandidateInfo]
    total: int


class SkillEvaluationInfo(BaseModel):
    """Skill evaluation information."""

    id: str
    candidate_id: str
    benchmark_id: str | None = None
    score: float | None = None
    passed: bool
    report: str | None = None
    evaluated_by: str | None = None
    created_at: datetime


class SkillReleaseInfo(BaseModel):
    """Skill release information."""

    id: str
    skill_key: str
    candidate_id: str
    version: int
    stage: SkillReleaseStage
    is_active: bool
    promoted_by: str | None = None
    promoted_at: datetime
    rollback_of: str | None = None


class SkillReleaseList(BaseModel):
    """Skill release list response."""

    items: list[SkillReleaseInfo]
    total: int


# Internal request models (not exported)


class _CreateSandboxRequest(BaseModel):
    """Internal: Create sandbox request body."""

    profile: str = "python-default"
    cargo_id: str | None = None
    ttl: int | None = None


class _ExtendTTLRequest(BaseModel):
    """Internal: Extend TTL request body."""

    extend_by: int = Field(..., ge=1)


class _PythonExecRequest(BaseModel):
    """Internal: Python exec request body."""

    code: str
    timeout: int = Field(default=30, ge=1, le=300)


class _ShellExecRequest(BaseModel):
    """Internal: Shell exec request body."""

    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: str | None = None


class _FileWriteRequest(BaseModel):
    """Internal: File write request body."""

    path: str
    content: str


class _CreateCargoRequest(BaseModel):
    """Internal: Create cargo request body."""

    size_limit_mb: int | None = Field(default=None, ge=1, le=65536)
