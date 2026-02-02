"""Capabilities API endpoints (through Sandbox).

These endpoints route capability requests to the runtime adapters.
See: plans/phase-1/capability-adapter-design.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from app.api.dependencies import (
    FilesystemCapabilityDep,
    PythonCapabilityDep,
    SandboxManagerDep,
    ShellCapabilityDep,
)
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


class ShellExecRequest(BaseModel):
    """Request to execute shell command."""

    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: str | None = None  # Relative to /workspace, validated

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


@router.post("/{sandbox_id}/python/exec", response_model=PythonExecResponse)
async def exec_python(
    request: PythonExecRequest,
    sandbox: PythonCapabilityDep,  # Validates python capability at profile level
    sandbox_mgr: SandboxManagerDep,
) -> PythonExecResponse:
    """Execute Python code in sandbox.

    This will:
    1. Validate profile supports python capability (via dependency)
    2. Ensure sandbox has a running session (auto-start if needed)
    3. Route execution to Ship runtime
    4. Return results
    """
    capability_router = CapabilityRouter(sandbox_mgr)

    result = await capability_router.exec_python(
        sandbox=sandbox,
        code=request.code,
        timeout=request.timeout,
    )

    return PythonExecResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        data=result.data,
    )


@router.post("/{sandbox_id}/shell/exec", response_model=ShellExecResponse)
async def exec_shell(
    request: ShellExecRequest,
    sandbox: ShellCapabilityDep,  # Validates shell capability at profile level
    sandbox_mgr: SandboxManagerDep,
) -> ShellExecResponse:
    """Execute shell command in sandbox."""
    capability_router = CapabilityRouter(sandbox_mgr)

    result = await capability_router.exec_shell(
        sandbox=sandbox,
        command=request.command,
        timeout=request.timeout,
        cwd=request.cwd,
    )

    return ShellExecResponse(
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
