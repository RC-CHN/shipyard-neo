"""Capabilities API endpoints (through Sandbox).

These endpoints route capability requests to the runtime adapters.
See: plans/phase-1/capability-adapter-design.md
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from app.api.dependencies import (
    AuthDep,
    BrowserCapabilityDep,
    FilesystemCapabilityDep,
    PythonCapabilityDep,
    SandboxManagerDep,
    ShellCapabilityDep,
    SkillLifecycleServiceDep,
)
from app.models.skill import ExecutionType
from app.router.capability import CapabilityRouter
from app.validators.path import (
    validate_optional_relative_path,
    validate_relative_path,
)

router = APIRouter()


# -- Path validation dependencies --


def validated_path(
    path: str = Query(..., description="File path relative to /workspace"),
) -> str:
    """Dependency to validate required path query parameter."""
    return validate_relative_path(path, field_name="path")


def validated_path_with_default(
    path: str = Query(".", description="Directory path relative to /workspace"),
) -> str:
    """Dependency to validate optional path query parameter with default."""
    return validate_relative_path(path, field_name="path")


# Type aliases for validated path dependencies
ValidatedPath = Annotated[str, Depends(validated_path)]
ValidatedPathWithDefault = Annotated[str, Depends(validated_path_with_default)]


# Request/Response Models


class PythonExecRequest(BaseModel):
    """Request to execute Python code."""

    code: str
    timeout: int = Field(default=30, ge=1, le=300)
    include_code: bool = False
    description: str | None = None
    tags: str | None = None


class PythonExecResponse(BaseModel):
    """Python execution response.

    `data` contains rich output from IPython kernel:
    {
        "execution_count": int | None,
        "output": {
            "text": str,
            "images": list[dict[str, str]]  # [{"image/png": "base64..."}]
        }
    }
    """

    success: bool
    output: str
    error: str | None = None
    data: dict[str, Any] | None = None
    execution_id: str | None = None
    execution_time_ms: int | None = None
    code: str | None = None


class ShellExecRequest(BaseModel):
    """Request to execute shell command."""

    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: str | None = None  # Relative to /workspace, validated
    include_code: bool = False
    description: str | None = None
    tags: str | None = None

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, v: str | None) -> str | None:
        """Validate cwd path if provided."""
        return validate_optional_relative_path(v, field_name="cwd")


class ShellExecResponse(BaseModel):
    """Shell execution response."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    execution_id: str | None = None
    execution_time_ms: int | None = None
    command: str | None = None


class FileReadRequest(BaseModel):
    """Request to read a file."""

    path: str  # Relative to /workspace


class FileReadResponse(BaseModel):
    """File read response."""

    content: str


class FileWriteRequest(BaseModel):
    """Request to write a file."""

    path: str  # Relative to /workspace, validated
    content: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Validate file path."""
        return validate_relative_path(v, field_name="path")


class FileListRequest(BaseModel):
    """Request to list directory."""

    path: str = "."  # Relative to /workspace


class FileListResponse(BaseModel):
    """File list response."""

    entries: list[dict[str, Any]]


class FileDeleteRequest(BaseModel):
    """Request to delete file/directory."""

    path: str  # Relative to /workspace


# Endpoints


class BrowserExecRequest(BaseModel):
    """Request to execute browser automation command."""

    cmd: str
    timeout: int = Field(default=30, ge=1, le=300)


class BrowserExecResponse(BaseModel):
    """Browser execution response (CLI passthrough)."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None


@router.post("/{sandbox_id}/python/exec", response_model=PythonExecResponse)
async def exec_python(
    request: PythonExecRequest,
    sandbox: PythonCapabilityDep,  # Validates python capability at profile level
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> PythonExecResponse:
    """Execute Python code in sandbox.

    This will:
    1. Validate profile supports python capability (via dependency)
    2. Ensure sandbox has a running session (auto-start if needed)
    3. Route execution to Ship runtime
    4. Return results
    """
    capability_router = CapabilityRouter(sandbox_mgr)

    start = time.perf_counter()
    result = await capability_router.exec_python(
        sandbox=sandbox,
        code=request.code,
        timeout=request.timeout,
    )
    execution_time_ms = int((time.perf_counter() - start) * 1000)
    current_session = await sandbox_mgr.get_current_session(sandbox)

    execution_entry = await skill_svc.create_execution(
        owner=owner,
        sandbox_id=sandbox.id,
        session_id=current_session.id if current_session else None,
        exec_type=ExecutionType.PYTHON,
        code=request.code,
        success=result.success,
        execution_time_ms=execution_time_ms,
        output=result.output,
        error=result.error,
        description=request.description,
        tags=request.tags,
    )

    return PythonExecResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        data=result.data,
        execution_id=execution_entry.id,
        execution_time_ms=execution_time_ms,
        code=request.code if request.include_code else None,
    )


@router.post("/{sandbox_id}/shell/exec", response_model=ShellExecResponse)
async def exec_shell(
    request: ShellExecRequest,
    sandbox: ShellCapabilityDep,  # Validates shell capability at profile level
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> ShellExecResponse:
    """Execute shell command in sandbox."""
    capability_router = CapabilityRouter(sandbox_mgr)

    start = time.perf_counter()
    result = await capability_router.exec_shell(
        sandbox=sandbox,
        command=request.command,
        timeout=request.timeout,
        cwd=request.cwd,
    )
    execution_time_ms = int((time.perf_counter() - start) * 1000)
    current_session = await sandbox_mgr.get_current_session(sandbox)

    execution_entry = await skill_svc.create_execution(
        owner=owner,
        sandbox_id=sandbox.id,
        session_id=current_session.id if current_session else None,
        exec_type=ExecutionType.SHELL,
        code=request.command,
        success=result.success,
        execution_time_ms=execution_time_ms,
        output=result.output,
        error=result.error,
        description=request.description,
        tags=request.tags,
    )

    return ShellExecResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        exit_code=result.exit_code,
        execution_id=execution_entry.id,
        execution_time_ms=execution_time_ms,
        command=request.command if request.include_code else None,
    )


@router.post("/{sandbox_id}/browser/exec", response_model=BrowserExecResponse)
async def exec_browser(
    request: BrowserExecRequest,
    sandbox: BrowserCapabilityDep,  # Validates browser capability at profile level
    sandbox_mgr: SandboxManagerDep,
) -> BrowserExecResponse:
    """Execute browser automation command in sandbox.

    Phase 2: Routes to Gull runtime via [`CapabilityRouter.exec_browser()`](pkgs/bay/app/router/capability/capability.py:213).
    """
    capability_router = CapabilityRouter(sandbox_mgr)

    result = await capability_router.exec_browser(
        sandbox=sandbox,
        cmd=request.cmd,
        timeout=request.timeout,
    )

    return BrowserExecResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        exit_code=result.exit_code,
    )


@router.get("/{sandbox_id}/filesystem/files", response_model=FileReadResponse)
async def read_file(
    sandbox: FilesystemCapabilityDep,  # Validates filesystem capability at profile level
    sandbox_mgr: SandboxManagerDep,
    path: ValidatedPath,  # Validated path dependency
) -> FileReadResponse:
    """Read file from sandbox."""
    capability_router = CapabilityRouter(sandbox_mgr)

    content = await capability_router.read_file(sandbox=sandbox, path=path)

    return FileReadResponse(content=content)


@router.put("/{sandbox_id}/filesystem/files", status_code=200)
async def write_file(
    request: FileWriteRequest,  # path validated by Pydantic field_validator
    sandbox: FilesystemCapabilityDep,  # Validates filesystem capability at profile level
    sandbox_mgr: SandboxManagerDep,
) -> dict[str, str]:
    """Write file to sandbox."""
    capability_router = CapabilityRouter(sandbox_mgr)

    await capability_router.write_file(
        sandbox=sandbox,
        path=request.path,
        content=request.content,
    )

    return {"status": "ok"}


@router.get("/{sandbox_id}/filesystem/directories", response_model=FileListResponse)
async def list_files(
    sandbox: FilesystemCapabilityDep,  # Validates filesystem capability at profile level
    sandbox_mgr: SandboxManagerDep,
    path: ValidatedPathWithDefault,  # Validated path with default "."
) -> FileListResponse:
    """List directory contents in sandbox."""
    capability_router = CapabilityRouter(sandbox_mgr)

    entries = await capability_router.list_files(sandbox=sandbox, path=path)

    return FileListResponse(entries=entries)


@router.delete("/{sandbox_id}/filesystem/files", status_code=200)
async def delete_file(
    sandbox: FilesystemCapabilityDep,  # Validates filesystem capability at profile level
    sandbox_mgr: SandboxManagerDep,
    path: ValidatedPath,  # Validated path dependency
) -> dict[str, str]:
    """Delete file or directory from sandbox."""
    capability_router = CapabilityRouter(sandbox_mgr)

    await capability_router.delete_file(sandbox=sandbox, path=path)

    return {"status": "ok"}


# -- Upload/Download endpoints (part of filesystem capability) --


class FileUploadResponse(BaseModel):
    """File upload response."""

    status: str
    path: str
    size: int


@router.post("/{sandbox_id}/filesystem/upload", response_model=FileUploadResponse)
async def upload_file(
    sandbox: FilesystemCapabilityDep,  # Validates filesystem capability at profile level
    sandbox_mgr: SandboxManagerDep,
    file: UploadFile = File(..., description="File to upload"),
    path: str = Form(..., description="Target path relative to /workspace"),
) -> FileUploadResponse:
    """Upload binary file to sandbox.

    This endpoint accepts multipart/form-data with:
    - file: The file to upload
    - path: Target path in the sandbox workspace
    """
    # Manually validate path for Form parameter
    validated_upload_path = validate_relative_path(path, field_name="path")

    capability_router = CapabilityRouter(sandbox_mgr)

    content = await file.read()
    await capability_router.upload_file(
        sandbox=sandbox, path=validated_upload_path, content=content
    )

    return FileUploadResponse(status="ok", path=validated_upload_path, size=len(content))


@router.get("/{sandbox_id}/filesystem/download")
async def download_file(
    sandbox: FilesystemCapabilityDep,  # Validates filesystem capability at profile level
    sandbox_mgr: SandboxManagerDep,
    path: ValidatedPath,  # Validated path dependency
) -> Response:
    """Download file from sandbox.

    Returns the file content as a binary stream.
    """
    capability_router = CapabilityRouter(sandbox_mgr)

    content = await capability_router.download_file(sandbox=sandbox, path=path)
    filename = Path(path).name

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
