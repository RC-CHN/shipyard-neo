"""Bay SDK error types.

Error codes are stable enums for programmatic handling.
Maps 1:1 with Bay API error codes.
"""

from __future__ import annotations

from typing import Any


class BayError(Exception):
    """Base error for all Bay SDK exceptions."""

    code: str = "internal_error"
    message: str = "An internal error occurred"
    status_code: int = 500

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(BayError):
    """Resource not found (404)."""

    code = "not_found"
    message = "Resource not found"
    status_code = 404


class UnauthorizedError(BayError):
    """Authentication required (401)."""

    code = "unauthorized"
    message = "Authentication required"
    status_code = 401


class ForbiddenError(BayError):
    """Permission denied (403)."""

    code = "forbidden"
    message = "Permission denied"
    status_code = 403


class QuotaExceededError(BayError):
    """Quota or rate limit exceeded (429)."""

    code = "quota_exceeded"
    message = "Quota exceeded"
    status_code = 429


class ConflictError(BayError):
    """Conflict (idempotency key or state conflict) (409)."""

    code = "conflict"
    message = "Conflict"
    status_code = 409


class ValidationError(BayError):
    """Request validation error (400)."""

    code = "validation_error"
    message = "Validation error"
    status_code = 400


class SessionNotReadyError(BayError):
    """Session is starting or not ready yet (503)."""

    code = "session_not_ready"
    message = "Session is starting"
    status_code = 503


class RequestTimeoutError(BayError):
    """Operation timed out (504).

    Note: Renamed from TimeoutError to avoid shadowing Python's builtin.
    """

    code = "timeout"
    message = "Operation timed out"
    status_code = 504


class ShipError(BayError):
    """Error from Ship runtime (502).

    Note: Renamed from RuntimeError to avoid shadowing Python's builtin.
    """

    code = "ship_error"
    message = "Runtime error"
    status_code = 502


class SandboxExpiredError(BayError):
    """Sandbox TTL already expired; cannot be extended (409)."""

    code = "sandbox_expired"
    message = "Sandbox is expired, cannot extend TTL"
    status_code = 409


class SandboxTTLInfiniteError(BayError):
    """Sandbox has infinite TTL; extending is meaningless (409)."""

    code = "sandbox_ttl_infinite"
    message = "Sandbox TTL is infinite, cannot extend TTL"
    status_code = 409


class CapabilityNotSupportedError(BayError):
    """Capability not supported by runtime (400)."""

    code = "capability_not_supported"
    message = "Capability not supported by runtime"
    status_code = 400


class InvalidPathError(BayError):
    """Invalid file path (400).

    Raised when a path fails validation:
    - Empty path
    - Absolute path (starts with /)
    - Path traversal (escapes workspace boundary)
    - Contains null bytes
    """

    code = "invalid_path"
    message = "Invalid path"
    status_code = 400


class CargoFileNotFoundError(BayError):
    """File not found in sandbox workspace (404).

    Applies to any relative path that doesn't exist, not just cargo scenarios.

    Note: Renamed from FileNotFoundError to avoid shadowing Python's builtin.
    """

    code = "file_not_found"
    message = "File not found"
    status_code = 404


# Error code to exception class mapping
ERROR_CODE_MAP: dict[str, type[BayError]] = {
    "not_found": NotFoundError,
    "unauthorized": UnauthorizedError,
    "forbidden": ForbiddenError,
    "quota_exceeded": QuotaExceededError,
    "conflict": ConflictError,
    "validation_error": ValidationError,
    "session_not_ready": SessionNotReadyError,
    "timeout": RequestTimeoutError,
    "ship_error": ShipError,
    "sandbox_expired": SandboxExpiredError,
    "sandbox_ttl_infinite": SandboxTTLInfiniteError,
    "capability_not_supported": CapabilityNotSupportedError,
    "invalid_path": InvalidPathError,
    "file_not_found": CargoFileNotFoundError,
}


def raise_for_error_response(
    status_code: int,
    response_body: dict[str, Any],
) -> None:
    """Raise appropriate BayError based on API error response.

    Args:
        status_code: HTTP status code
        response_body: Parsed JSON response body

    Raises:
        BayError: Appropriate subclass based on error code
    """
    error_data = response_body.get("error", {})
    code = error_data.get("code", "internal_error")
    message = error_data.get("message")
    details = error_data.get("details", {})

    error_class = ERROR_CODE_MAP.get(code, BayError)
    raise error_class(message=message, details=details)
