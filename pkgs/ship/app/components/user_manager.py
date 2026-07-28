"""
Runtime execution utilities for fixed-user shell operations.

This module provides utilities for running commands as the fixed 'shipyard' user,
with background process management and interactive shell support.
"""

import asyncio
import logging
import os
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from fastapi import HTTPException

from ..workspace import resolve_path

logger = logging.getLogger(__name__)

# 固定的执行用户和 workspace
EXEC_USER = "shipyard"
WORKSPACE_ROOT = Path("/workspace")

# 后台进程注册表：process_id -> BackgroundProcessEntry
_background_processes: Dict[str, "BackgroundProcessEntry"] = {}


@dataclass
class ProcessResult:
    success: bool
    stdout: str
    stderr: str
    return_code: Optional[int] = None
    pid: Optional[int] = None
    process_id: Optional[str] = None
    error: Optional[str] = None


class BackgroundProcessEntry:
    """后台进程条目"""

    def __init__(
        self,
        process_id: str,
        pid: int,
        command: str,
        process: asyncio.subprocess.Process,
    ):
        self.process_id = process_id
        self.pid = pid
        self.command = command
        self.process = process

    @property
    def status(self) -> str:
        """获取进程状态"""
        if self.process.returncode is None:
            return "running"
        elif self.process.returncode == 0:
            return "completed"
        else:
            return "failed"


def generate_process_id() -> str:
    """生成进程ID"""
    return str(uuid.uuid4())[:8]


def register_background_process(
    process_id: str,
    pid: int,
    command: str,
    process: asyncio.subprocess.Process,
) -> None:
    """注册后台进程"""
    _background_processes[process_id] = BackgroundProcessEntry(
        process_id=process_id,
        pid=pid,
        command=command,
        process=process,
    )
    logger.info(
        "Registered background process: process_id=%s pid=%s",
        process_id,
        pid,
    )


def _cleanup_completed_processes() -> int:
    """清理已完成的后台进程条目，返回清理数量。

    内部函数，由 get_background_processes() 调用。
    清理策略：删除所有 returncode is not None 的条目。
    """
    completed_ids = [
        process_id
        for process_id, entry in _background_processes.items()
        if entry.process.returncode is not None
    ]
    for process_id in completed_ids:
        del _background_processes[process_id]

    if completed_ids:
        logger.debug(
            "Cleaned up %d completed background processes: %s",
            len(completed_ids),
            completed_ids,
        )
    return len(completed_ids)


def get_background_processes() -> List[Dict]:
    """获取所有后台进程。

    注意：此函数会自动清理已完成的进程条目，防止内存泄露。
    """
    # 先清理已完成的进程
    _cleanup_completed_processes()

    processes = []
    for entry in _background_processes.values():
        processes.append(
            {
                "process_id": entry.process_id,
                "pid": entry.pid,
                "command": entry.command,
                "status": entry.status,
            }
        )
    return processes


def get_background_process(process_id: str) -> Optional[Dict]:
    """获取指定后台进程"""
    entry = _background_processes.get(process_id)
    if entry:
        return {
            "process_id": entry.process_id,
            "pid": entry.pid,
            "command": entry.command,
            "status": entry.status,
        }
    return None


def _get_env_file_source() -> str:
    """获取环境文件 source 语句"""
    env_file = WORKSPACE_ROOT / ".bay_env.sh"
    if env_file.exists():
        return f"source {env_file} && "
    return ""


async def start_interactive_shell(
    cols: int = 80,
    rows: int = 24,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, int]:
    """
    启动交互式 shell (PTY)

    Returns:
        (master_fd, pid)
    """
    try:
        import pty
        import termios
        import struct
        import fcntl

        # 准备环境变量
        process_env = {
            "HOME": str(WORKSPACE_ROOT),
            "USER": EXEC_USER,
            "LOGNAME": EXEC_USER,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
        }
        if env:
            process_env.update(env)

        pid, master_fd = pty.fork()

        if pid == 0:  # Child process
            try:
                # 设置工作目录
                os.chdir(str(WORKSPACE_ROOT))

                # 获取环境变量注入 source 语句
                source_cmd = _get_env_file_source()

                # 准备 sudo 命令参数
                sudo_cmd = "/usr/bin/sudo"
                sudo_args = [
                    sudo_cmd,
                    "-u",
                    EXEC_USER,
                    "-H",
                    "bash",
                    "-l",
                    "-c",
                    f"{source_cmd}exec bash -l",  # source 环境变量后替换为 login shell
                ]

                os.execvpe(sudo_cmd, sudo_args, process_env)

            except Exception as e:
                print(f"Error starting shell: {e}")
                os._exit(1)

        # Parent process
        # 设置窗口大小
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        logger.info(f"Started interactive shell for {EXEC_USER} (PID {pid})")
        return master_fd, pid

    except Exception as e:
        logger.error(f"Failed to start interactive shell: {e}")
        raise


async def run_command(
    command: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    shell: bool = True,
    background: bool = False,
) -> ProcessResult:
    """以 shipyard 用户身份运行命令"""
    try:
        # 准备环境变量
        process_env = {
            "HOME": str(WORKSPACE_ROOT),
            "USER": EXEC_USER,
            "LOGNAME": EXEC_USER,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "SHELL": "/bin/bash",
        }
        if env:
            process_env.update(env)

        # Keep command text opaque; only the explicit cwd is interpreted as a
        # path and therefore goes through the same policy as filesystem APIs.
        working_dir = resolve_path(cwd or ".")

        env_args = []
        if env:
            for key, value in env.items():
                env_args.append(f"{key}={value}")

        # 获取环境变量注入 source 语句
        source_cmd = _get_env_file_source()

        if shell:
            sudo_args = [
                "sudo",
                "-u",
                EXEC_USER,
                "-H",
            ]
            if env_args:
                sudo_args.extend(["env", *env_args])
            sudo_args.extend(
                [
                    "bash",
                    "-lc",
                    f"{source_cmd}cd {shlex.quote(str(working_dir))} && {command}",
                ]
            )
            logger.debug(
                "Shell exec args: %s env_keys=%s",
                sudo_args,
                list(env.keys()) if env else [],
            )
            process = await asyncio.create_subprocess_exec(
                *sudo_args,
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            args = shlex.split(command)
            sudo_args = [
                "sudo",
                "-u",
                EXEC_USER,
                "-H",
            ]
            if env_args:
                sudo_args.extend(["env", *env_args])
            sudo_args.extend(args)
            logger.debug(
                "Exec args: %s env_keys=%s",
                sudo_args,
                list(env.keys()) if env else [],
            )
            process = await asyncio.create_subprocess_exec(
                *sudo_args,
                env=process_env,
                cwd=str(working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        if background:
            process_id = generate_process_id()
            register_background_process(
                process_id=process_id,
                pid=process.pid,
                command=command,
                process=process,
            )
            logger.info(
                "Background shell exec started: user=%s pid=%s process_id=%s cmd=%s",
                EXEC_USER,
                process.pid,
                process_id,
                command,
            )
            return ProcessResult(
                success=True,
                return_code=0,
                stdout="",
                stderr="",
                pid=process.pid,
                process_id=process_id,
            )
        else:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                return ProcessResult(
                    success=process.returncode == 0,
                    return_code=process.returncode,
                    stdout=stdout.decode().strip(),
                    stderr=stderr.decode().strip(),
                    pid=process.pid,
                    process_id=None,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return ProcessResult(
                    success=False,
                    return_code=-1,
                    stdout="",
                    stderr="",
                    pid=process.pid,
                    process_id=None,
                    error="Command timed out",
                )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Shell exec failed: cmd=%s cwd=%s env_keys=%s",
            command,
            cwd,
            list(env.keys()) if env else [],
        )
        return ProcessResult(
            success=False,
            return_code=-1,
            stdout="",
            stderr="",
            error=str(e),
            pid=None,
            process_id=None,
        )
