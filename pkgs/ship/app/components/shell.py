from typing import Dict, Optional, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .user_manager import run_command, get_background_processes

router = APIRouter()


class ExecuteShellRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    timeout: Optional[int] = 30
    shell: bool = True
    background: bool = False


class ExecuteShellResponse(BaseModel):
    success: bool
    return_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    pid: Optional[int] = None
    process_id: Optional[str] = None  # 用于后台进程
    error: Optional[str] = None


class ProcessInfo(BaseModel):
    process_id: str
    pid: int
    command: str
    status: str


class ProcessListResponse(BaseModel):
    processes: List[ProcessInfo]


@router.post("/exec", response_model=ExecuteShellResponse)
async def execute_shell_command(request: ExecuteShellRequest):
    """执行Shell命令"""
    try:
        result = await run_command(
            command=request.command,
            cwd=request.cwd,
            env=request.env,
            timeout=request.timeout,
            shell=request.shell,
            background=request.background,
        )

        return ExecuteShellResponse(**result.__dict__)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to execute command: {str(e)}"
        )


@router.get("/processes", response_model=ProcessListResponse)
async def list_background_processes():
    """获取所有后台进程列表"""
    processes = get_background_processes()
    return ProcessListResponse(
        processes=[
            ProcessInfo(
                process_id=p["process_id"],
                pid=p["pid"],
                command=p["command"],
                status=p["status"],
            )
            for p in processes
        ]
    )
