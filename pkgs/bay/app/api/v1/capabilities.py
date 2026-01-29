"""Capabilities API endpoints (through Sandbox).

These endpoints route capability requests to the runtime adapters.
See: plans/phase-1/capability-adapter-design.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.dependencies import AuthDep, SandboxManagerDep
from app.router.capability import CapabilityRouter

router = APIRouter()


# Request/Response Models


class PythonExecRequest(BaseModel):
    """Request to execute Python code."""

    code: str
    timeout: int = Field(default=30, ge=1, le=300)


class PythonExecResponse(BaseModel):
    """Python execution response."""

    success: bool
    output: str
    error: str | None = None
    data: dict[str, Any] | None = None


class ShellExecRequest(BaseModel):
    """Request to execute shell command."""

    command: str
    timeout: int = Field(default=30, ge=1, le=300)
    cwd: str | None = None  # Relative to /workspace


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

    path: str  # Relative to /workspace
    content: str


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
    sandbox_id: str,
    request: PythonExecRequest,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> PythonExecResponse:
    """Execute Python code in sandbox.
    
    This will:
    1. Ensure sandbox has a running session (auto-start if needed)
    2. Route execution to Ship runtime
    3. Return results
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
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
    sandbox_id: str,
    request: ShellExecRequest,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> ShellExecResponse:
    """Execute shell command in sandbox."""
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
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
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
    path: str = Query(..., description="File path relative to /workspace"),
) -> FileReadResponse:
    """Read file from sandbox."""
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)

    content = await capability_router.read_file(sandbox=sandbox, path=path)

    return FileReadResponse(content=content)


@router.put("/{sandbox_id}/filesystem/files", status_code=200)
async def write_file(
    sandbox_id: str,
    request: FileWriteRequest,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
) -> dict[str, str]:
    """Write file to sandbox."""
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)

    await capability_router.write_file(
        sandbox=sandbox,
        path=request.path,
        content=request.content,
    )

    return {"status": "ok"}


@router.get("/{sandbox_id}/filesystem/directories", response_model=FileListResponse)
async def list_files(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
    path: str = Query(".", description="Directory path relative to /workspace"),
) -> FileListResponse:
    """List directory contents in sandbox."""
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)

    entries = await capability_router.list_files(sandbox=sandbox, path=path)

    return FileListResponse(entries=entries)


@router.delete("/{sandbox_id}/filesystem/files", status_code=200)
async def delete_file(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
    path: str = Query(..., description="File/directory path relative to /workspace"),
) -> dict[str, str]:
    """Delete file or directory from sandbox."""
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
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
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
    file: UploadFile = File(..., description="File to upload"),
    path: str = Form(..., description="Target path relative to /workspace"),
) -> FileUploadResponse:
    """Upload binary file to sandbox.
    
    This endpoint accepts multipart/form-data with:
    - file: The file to upload
    - path: Target path in the sandbox workspace
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)

    content = await file.read()
    await capability_router.upload_file(sandbox=sandbox, path=path, content=content)

    return FileUploadResponse(status="ok", path=path, size=len(content))


@router.get("/{sandbox_id}/filesystem/download")
async def download_file(
    sandbox_id: str,
    sandbox_mgr: SandboxManagerDep,
    owner: AuthDep,
    path: str = Query(..., description="File path relative to /workspace"),
) -> Response:
    """Download file from sandbox.
    
    Returns the file content as a binary stream.
    """
    sandbox = await sandbox_mgr.get(sandbox_id, owner)
    capability_router = CapabilityRouter(sandbox_mgr)

    content = await capability_router.download_file(sandbox=sandbox, path=path)
    filename = Path(path).name

    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
