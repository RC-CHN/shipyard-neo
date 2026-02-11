"""Shipyard Neo Python SDK.

A Python client for the Bay API - secure sandbox execution for AI agents.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

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
    BrowserBatchExecResult,
    BrowserBatchStepResult,
    BrowserExecResult,
    BrowserSkillRunResult,
    CargoInfo,
    CargoList,
    ContainerInfo,
    ExecutionHistoryEntry,
    ExecutionHistoryList,
    FileInfo,
    ProfileInfo,
    ProfileList,
    PythonExecResult,
    RuntimeContainerInfo,
    SandboxInfo,
    SandboxList,
    SandboxStatus,
    ShellExecResult,
    SkillCandidateInfo,
    SkillCandidateList,
    SkillCandidateStatus,
    SkillEvaluationInfo,
    SkillReleaseHealth,
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
    "ProfileInfo",
    "ProfileList",
    "ContainerInfo",
    "RuntimeContainerInfo",
    "PythonExecResult",
    "ShellExecResult",
    "BrowserExecResult",
    "BrowserBatchStepResult",
    "BrowserBatchExecResult",
    "BrowserSkillRunResult",
    "SkillCandidateStatus",
    "SkillReleaseStage",
    "SkillCandidateInfo",
    "SkillCandidateList",
    "SkillEvaluationInfo",
    "SkillReleaseInfo",
    "SkillReleaseHealth",
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

try:
    __version__ = _pkg_version("shipyard-neo-sdk")
except PackageNotFoundError:
    __version__ = "unknown"
