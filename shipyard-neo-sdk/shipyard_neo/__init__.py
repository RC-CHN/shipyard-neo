"""Shipyard Neo Python SDK.

A Python client for the Bay API - secure sandbox execution for AI agents.
"""

from shipyard_neo.client import BayClient
from shipyard_neo.errors import (
    BayError,
    CapabilityNotSupportedError,
    CargoFileNotFoundError,
    ConflictError,
    ForbiddenError,
    InvalidPathError,
    NotFoundError,
    QuotaExceededError,
    RequestTimeoutError,
    SandboxExpiredError,
    SandboxTTLInfiniteError,
    SessionNotReadyError,
    ShipError,
    UnauthorizedError,
    ValidationError,
)
from shipyard_neo.skills import SkillManager
from shipyard_neo.types import (
    CargoInfo,
    CargoList,
    ExecutionHistoryEntry,
    ExecutionHistoryList,
    FileInfo,
    PythonExecResult,
    SandboxInfo,
    SandboxList,
    SandboxStatus,
    ShellExecResult,
    SkillCandidateInfo,
    SkillCandidateList,
    SkillCandidateStatus,
    SkillEvaluationInfo,
    SkillReleaseInfo,
    SkillReleaseList,
    SkillReleaseStage,
)

__all__ = [
    # Client
    "BayClient",
    "SkillManager",
    # Types
    "SandboxStatus",
    "SandboxInfo",
    "SandboxList",
    "CargoInfo",
    "CargoList",
    "ExecutionHistoryEntry",
    "ExecutionHistoryList",
    "FileInfo",
    "PythonExecResult",
    "ShellExecResult",
    "SkillCandidateStatus",
    "SkillReleaseStage",
    "SkillCandidateInfo",
    "SkillCandidateList",
    "SkillEvaluationInfo",
    "SkillReleaseInfo",
    "SkillReleaseList",
    # Errors
    "BayError",
    "NotFoundError",
    "UnauthorizedError",
    "ForbiddenError",
    "QuotaExceededError",
    "ConflictError",
    "ValidationError",
    "SessionNotReadyError",
    "RequestTimeoutError",
    "ShipError",
    "SandboxExpiredError",
    "SandboxTTLInfiniteError",
    "CapabilityNotSupportedError",
    "InvalidPathError",
    "CargoFileNotFoundError",
]

__version__ = "0.1.0"
