"""Bay error types and exception handling.

Error codes are stable enums for programmatic handling.
See: plans/bay-api.md section 3
"""

from __future__ import annotations

from typing import Any


class BayError(Exception):
    """Base error for all Bay exceptions."""

    code: str = "internal_error"
    message: str = "An internal error occurred"
    status_code: int = 500

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.message = message or self.__class__.message
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        """Convert to API error response format."""
        error = {
            "code": self.code,
            "message": self.message,
        }
        if request_id:
            error["request_id"] = request_id
        if self.details:
            error["details"] = self.details
        return {"error": error}


class NotFoundError(BayError):
    """Resource not found or not visible."""

    code = "not_found"
    message = "Resource not found"
    status_code = 404


class UnauthorizedError(BayError):
    """Authentication required."""

    code = "unauthorized"
    message = "Authentication required"
    status_code = 401


class ForbiddenError(BayError):
    """Permission denied."""

    code = "forbidden"
    message = "Permission denied"
    status_code = 403


class QuotaExceededError(BayError):
    """Quota or rate limit exceeded."""

    code = "quota_exceeded"
    message = "Quota exceeded"
    status_code = 429


class SessionNotReadyError(BayError):
    """Session is starting or not ready yet."""

    code = "session_not_ready"
    message = "Session is starting"
    status_code = 503

    def __init__(
        self,
        message: str | None = None,
        sandbox_id: str | None = None,
        retry_after_ms: int | None = None,
    ):
        details = {}
        if sandbox_id:
            details["sandbox_id"] = sandbox_id
        if retry_after_ms:
            details["retry_after_ms"] = retry_after_ms
        super().__init__(message, details)


class RequestTimeoutError(BayError):
    """Operation timed out.

    Note: Renamed from TimeoutError to avoid shadowing Python's builtin TimeoutError.
    """

    code = "timeout"
    message = "Operation timed out"
    status_code = 504


class ShipError(BayError):
    """Error from Ship runtime."""

    code = "ship_error"
    message = "Runtime error"
    status_code = 502


class ConflictError(BayError):
    """Conflict (idempotency key or state conflict)."""

    code = "conflict"
    message = "Conflict"
    status_code = 409


class SandboxExpiredError(BayError):
    """Sandbox TTL already expired; cannot be extended (no resurrection)."""

    code = "sandbox_expired"
    message = "sandbox is expired, cannot extend ttl"
    status_code = 409


class SandboxTTLInfiniteError(BayError):
    """Sandbox has infinite TTL (expires_at is null); extending is meaningless."""

    code = "sandbox_ttl_infinite"
    message = "sandbox ttl is infinite, cannot extend ttl"
    status_code = 409


class ValidationError(BayError):
    """Request validation error."""

    code = "validation_error"
    message = "Validation error"
    status_code = 400


class CargoFileNotFoundError(BayError):
    """File not found in sandbox workspace.

    Note: Renamed from FileNotFoundError to avoid shadowing Python's builtin FileNotFoundError.
    """

    code = "file_not_found"
    message = "File not found"
    status_code = 404


class CapabilityNotSupportedError(BayError):
    """Runtime does not support requested capability."""

    code = "capability_not_supported"
    message = "Capability not supported by runtime"
    status_code = 400

    def __init__(
        self,
        message: str | None = None,
        capability: str | None = None,
        available: list[str] | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if capability:
            details["capability"] = capability
        if available is not None:
            details["available"] = available
        super().__init__(message, details)


class InvalidPathError(BayError):
    """Invalid file path (absolute, traversal, etc.).

    Raised when a path fails validation:
    - Empty path
    - Absolute path (starts with /)
    - Path traversal (escapes workspace boundary)
    - Contains null bytes
    """

    code = "invalid_path"
    message = "Invalid path"
    status_code = 400
