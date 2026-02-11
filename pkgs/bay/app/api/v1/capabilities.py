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
from app.errors import NotFoundError, ValidationError
from app.models.skill import ExecutionType, LearnStatus
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
    description: str | None = None
    tags: str | None = None
    learn: bool = False
    include_trace: bool = False


class BrowserExecResponse(BaseModel):
    """Browser execution response (CLI passthrough)."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    execution_id: str | None = None
    execution_time_ms: int | None = None
    trace_ref: str | None = None


class BrowserBatchExecRequest(BaseModel):
    """Request to execute a batch of browser automation commands."""

    commands: list[str] = Field(..., min_length=1)
    timeout: int = Field(default=60, ge=1, le=600)
    stop_on_error: bool = Field(default=True, description="Stop if a command fails")
    description: str | None = None
    tags: str | None = None
    learn: bool = False
    include_trace: bool = False


class BrowserBatchStepResult(BaseModel):
    """Result of a single step in a browser batch execution."""

    cmd: str
    stdout: str
    stderr: str
    exit_code: int
    step_index: int
    duration_ms: int = 0


class BrowserBatchExecResponse(BaseModel):
    """Browser batch execution response."""

    results: list[BrowserBatchStepResult]
    total_steps: int
    completed_steps: int
    success: bool
    duration_ms: int = 0
    execution_id: str | None = None
    execution_time_ms: int | None = None
    trace_ref: str | None = None


class BrowserSkillRunRequest(BaseModel):
    """Replay an active browser skill release."""

    timeout: int = Field(default=60, ge=1, le=600)
    stop_on_error: bool = Field(default=True)
    include_trace: bool = False
    description: str | None = None
    tags: str | None = None


class BrowserSkillRunResponse(BaseModel):
    """Browser skill replay response."""

    skill_key: str
    release_id: str
    execution_id: str
    execution_time_ms: int
    trace_ref: str | None = None
    results: list[BrowserBatchStepResult]
    total_steps: int
    completed_steps: int
    success: bool
    duration_ms: int = 0


class BrowserTraceResponse(BaseModel):
    """Trace payload lookup response."""

    trace_ref: str
    trace: dict[str, Any] | list[Any]


def _build_browser_exec_trace_payload(
    *,
    cmd: str,
    result_output: str,
    result_error: str | None,
    exit_code: int | None,
) -> dict[str, Any]:
    return {
        "kind": "browser_exec_trace",
        "steps": [
            {
                "kind": "individual_action",
                "cmd": cmd,
                "stdout": result_output,
                "stderr": result_error or "",
                "exit_code": int(exit_code if exit_code is not None else -1),
                "step_index": 0,
            }
        ],
    }


def _build_browser_batch_trace_payload(
    *,
    request_commands: list[str],
    raw_result: dict[str, Any],
) -> dict[str, Any]:
    raw_steps = raw_result.get("results", [])
    steps: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            continue
        default_cmd = request_commands[idx] if idx < len(request_commands) else ""
        exit_code = item.get("exit_code", -1)
        try:
            exit_code_int = int(exit_code)
        except Exception:
            exit_code_int = -1
        steps.append(
            {
                "kind": "individual_action",
                "cmd": item.get("cmd", default_cmd),
                "stdout": item.get("stdout", ""),
                "stderr": item.get("stderr", ""),
                "exit_code": exit_code_int,
                "step_index": int(item.get("step_index", idx)),
                "duration_ms": int(item.get("duration_ms", 0)),
            }
        )
    return {
        "kind": "browser_batch_trace",
        "steps": steps,
        "total_steps": int(raw_result.get("total_steps", len(request_commands))),
        "completed_steps": int(raw_result.get("completed_steps", len(steps))),
        "success": bool(raw_result.get("success", False)),
        "duration_ms": int(raw_result.get("duration_ms", 0)),
    }


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
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> BrowserExecResponse:
    """Execute browser automation command in sandbox.

    Routes to Gull runtime through CapabilityRouter.
    """
    capability_router = CapabilityRouter(sandbox_mgr)

    start = time.perf_counter()
    result = await capability_router.exec_browser(
        sandbox=sandbox,
        cmd=request.cmd,
        timeout=request.timeout,
    )
    execution_time_ms = int((time.perf_counter() - start) * 1000)

    stored_trace_ref: str | None = None
    trace_ref: str | None = None
    if request.include_trace or request.learn:
        trace_payload = _build_browser_exec_trace_payload(
            cmd=request.cmd,
            result_output=result.output,
            result_error=result.error,
            exit_code=result.exit_code,
        )
        trace_blob = await skill_svc.create_artifact_blob(
            owner=owner,
            kind="browser_trace",
            payload=trace_payload,
        )
        stored_trace_ref = skill_svc.make_blob_ref(trace_blob.id)
        if request.include_trace:
            trace_ref = stored_trace_ref

    current_session = await sandbox_mgr.get_current_session(sandbox)
    execution_entry = await skill_svc.create_execution(
        owner=owner,
        sandbox_id=sandbox.id,
        session_id=current_session.id if current_session else None,
        exec_type=ExecutionType.BROWSER,
        code=request.cmd,
        success=result.success,
        execution_time_ms=execution_time_ms,
        output=result.output,
        error=result.error,
        payload_ref=stored_trace_ref,
        description=request.description,
        tags=request.tags,
        learn_enabled=request.learn,
        learn_status=LearnStatus.PENDING if request.learn else LearnStatus.SKIPPED,
    )

    return BrowserExecResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        exit_code=result.exit_code,
        execution_id=execution_entry.id,
        execution_time_ms=execution_time_ms,
        trace_ref=trace_ref,
    )


@router.post("/{sandbox_id}/browser/exec_batch", response_model=BrowserBatchExecResponse)
async def exec_browser_batch(
    request: BrowserBatchExecRequest,
    sandbox: BrowserCapabilityDep,  # Validates browser capability at profile level
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> BrowserBatchExecResponse:
    """Execute a batch of browser automation commands in sandbox.

    Phase 2: Routes to Gull runtime batch endpoint.
    Records as a single execution history entry with exec_type=browser_batch.
    """
    capability_router = CapabilityRouter(sandbox_mgr)

    start = time.perf_counter()
    raw_result = await capability_router.exec_browser_batch(
        sandbox=sandbox,
        commands=request.commands,
        timeout=request.timeout,
        stop_on_error=request.stop_on_error,
    )
    execution_time_ms = int((time.perf_counter() - start) * 1000)

    # Parse raw result into response model
    results = [
        BrowserBatchStepResult(
            cmd=r.get("cmd", ""),
            stdout=r.get("stdout", ""),
            stderr=r.get("stderr", ""),
            exit_code=r.get("exit_code", -1),
            step_index=r.get("step_index", i),
            duration_ms=r.get("duration_ms", 0),
        )
        for i, r in enumerate(raw_result.get("results", []))
    ]

    total_steps = raw_result.get("total_steps", len(request.commands))
    completed_steps = raw_result.get("completed_steps", len(results))
    success = raw_result.get("success", False)
    batch_duration_ms = raw_result.get("duration_ms", execution_time_ms)

    stored_trace_ref: str | None = None
    trace_ref: str | None = None
    if request.include_trace or request.learn:
        trace_payload = _build_browser_batch_trace_payload(
            request_commands=request.commands,
            raw_result=raw_result,
        )
        trace_blob = await skill_svc.create_artifact_blob(
            owner=owner,
            kind="browser_trace",
            payload=trace_payload,
        )
        stored_trace_ref = skill_svc.make_blob_ref(trace_blob.id)
        if request.include_trace:
            trace_ref = stored_trace_ref

    # Record as single execution history entry
    combined_code = "\n".join(request.commands)
    combined_output = "\n".join(r.stdout.strip() for r in results if r.stdout.strip())
    combined_error = "\n".join(r.stderr.strip() for r in results if r.stderr.strip()) or None

    current_session = await sandbox_mgr.get_current_session(sandbox)
    execution_entry = await skill_svc.create_execution(
        owner=owner,
        sandbox_id=sandbox.id,
        session_id=current_session.id if current_session else None,
        exec_type=ExecutionType.BROWSER_BATCH,
        code=combined_code,
        success=success,
        execution_time_ms=execution_time_ms,
        output=combined_output,
        error=combined_error,
        payload_ref=stored_trace_ref,
        description=request.description,
        tags=request.tags,
        learn_enabled=request.learn,
        learn_status=LearnStatus.PENDING if request.learn else LearnStatus.SKIPPED,
    )

    return BrowserBatchExecResponse(
        results=results,
        total_steps=total_steps,
        completed_steps=completed_steps,
        success=success,
        duration_ms=batch_duration_ms,
        execution_id=execution_entry.id,
        execution_time_ms=execution_time_ms,
        trace_ref=trace_ref,
    )


@router.post(
    "/{sandbox_id}/browser/skills/{skill_key}/run",
    response_model=BrowserSkillRunResponse,
)
async def run_browser_skill(
    sandbox_id: str,
    skill_key: str,
    request: BrowserSkillRunRequest,
    sandbox: BrowserCapabilityDep,
    sandbox_mgr: SandboxManagerDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> BrowserSkillRunResponse:
    """Replay active browser skill payload in sandbox."""
    _ = sandbox_id  # validated by BrowserCapabilityDep
    capability_router = CapabilityRouter(sandbox_mgr)

    release = await skill_svc.get_active_release(owner=owner, skill_key=skill_key)
    if release is None:
        raise NotFoundError(f"No active release found for skill_key: {skill_key}")

    # Eagerly capture ORM scalar attributes *before* calling exec_browser_batch,
    # which internally calls ensure_running → rollback/commit and expires all
    # objects attached to the shared DB session (MissingGreenlet otherwise).
    release_id = release.id
    release_stage_value = release.stage.value

    candidate = await skill_svc.get_candidate(owner=owner, candidate_id=release.candidate_id)
    payload = await skill_svc.get_payload_by_ref(
        owner=owner,
        payload_ref=candidate.payload_ref,
    )
    if not isinstance(payload, dict):
        raise ValidationError("Candidate payload must be a JSON object")
    commands_raw = payload.get("commands")
    if not isinstance(commands_raw, list):
        raise ValidationError("Candidate payload missing 'commands' array")
    commands = [str(cmd) for cmd in commands_raw if str(cmd).strip()]
    if not commands:
        raise ValidationError("Candidate payload commands must not be empty")

    start = time.perf_counter()
    raw_result = await capability_router.exec_browser_batch(
        sandbox=sandbox,
        commands=commands,
        timeout=request.timeout,
        stop_on_error=request.stop_on_error,
    )
    execution_time_ms = int((time.perf_counter() - start) * 1000)

    results = [
        BrowserBatchStepResult(
            cmd=item.get("cmd", ""),
            stdout=item.get("stdout", ""),
            stderr=item.get("stderr", ""),
            exit_code=item.get("exit_code", -1),
            step_index=item.get("step_index", idx),
            duration_ms=item.get("duration_ms", 0),
        )
        for idx, item in enumerate(raw_result.get("results", []))
        if isinstance(item, dict)
    ]
    total_steps = int(raw_result.get("total_steps", len(commands)))
    completed_steps = int(raw_result.get("completed_steps", len(results)))
    success = bool(raw_result.get("success", False))
    duration_ms = int(raw_result.get("duration_ms", execution_time_ms))

    trace_ref: str | None = None
    if request.include_trace:
        trace_blob = await skill_svc.create_artifact_blob(
            owner=owner,
            kind="browser_trace",
            payload=_build_browser_batch_trace_payload(
                request_commands=commands,
                raw_result=raw_result,
            ),
        )
        trace_ref = skill_svc.make_blob_ref(trace_blob.id)

    merged_tags = skill_svc.merge_tags(
        request.tags,
        f"skill:{skill_key}",
        f"release:{release_id}",
        f"stage:{release_stage_value}",
    )
    current_session = await sandbox_mgr.get_current_session(sandbox)
    execution = await skill_svc.create_execution(
        owner=owner,
        sandbox_id=sandbox.id,
        session_id=current_session.id if current_session else None,
        exec_type=ExecutionType.BROWSER_BATCH,
        code="\n".join(commands),
        success=success,
        execution_time_ms=execution_time_ms,
        output="\n".join(item.stdout.strip() for item in results if item.stdout.strip()),
        error=("\n".join(item.stderr.strip() for item in results if item.stderr.strip()) or None),
        payload_ref=trace_ref,
        description=request.description,
        tags=merged_tags,
        learn_enabled=False,
        learn_status=LearnStatus.SKIPPED,
    )

    return BrowserSkillRunResponse(
        skill_key=skill_key,
        release_id=release_id,
        execution_id=execution.id,
        execution_time_ms=execution_time_ms,
        trace_ref=trace_ref,
        results=results,
        total_steps=total_steps,
        completed_steps=completed_steps,
        success=success,
        duration_ms=duration_ms,
    )


@router.get(
    "/{sandbox_id}/browser/traces/{trace_ref}",
    response_model=BrowserTraceResponse,
)
async def get_browser_trace(
    sandbox_id: str,
    trace_ref: str,
    sandbox: BrowserCapabilityDep,
    skill_svc: SkillLifecycleServiceDep,
    owner: AuthDep,
) -> BrowserTraceResponse:
    """Get browser trace payload by trace reference."""
    _ = sandbox_id
    _ = sandbox  # ensure caller has browser capability and sandbox ownership
    _blob, trace = await skill_svc.get_payload_with_blob_by_ref(
        owner=owner,
        payload_ref=trace_ref,
    )
    return BrowserTraceResponse(trace_ref=trace_ref, trace=trace)


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
