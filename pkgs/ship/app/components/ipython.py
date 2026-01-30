import asyncio
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from jupyter_client.manager import AsyncKernelManager
from ..workspace import get_workspace_dir, WORKSPACE_ROOT

router = APIRouter()

# 单例内核管理器
_kernel_manager: Optional[AsyncKernelManager] = None


class ExecuteCodeRequest(BaseModel):
    code: str
    timeout: int = 30
    silent: bool = False


class ExecuteCodeResponse(BaseModel):
    success: bool
    execution_count: Optional[int] = None
    output: dict = {}
    error: Optional[str] = None


class KernelStatusResponse(BaseModel):
    status: str
    has_kernel: bool
    workspace: str


async def get_or_create_kernel() -> AsyncKernelManager:
    """获取或创建单例内核管理器"""
    global _kernel_manager
    if _kernel_manager is None:
        # 确保 workspace 目录存在
        workspace_dir = get_workspace_dir()

        # 创建新的内核管理器，在启动时设置工作目录
        km: AsyncKernelManager = AsyncKernelManager()
        # 通过 cwd 参数在启动时设置工作目录
        await km.start_kernel(cwd=str(workspace_dir))
        _kernel_manager = km

        # 执行静态初始化代码（字体配置等）
        await _init_kernel_matplotlib(km)

    return _kernel_manager


async def ensure_kernel_running(km: AsyncKernelManager):
    """确保内核正在运行"""
    if not km.has_kernel or not await km.is_alive():
        workspace_dir = get_workspace_dir()
        await km.start_kernel(cwd=str(workspace_dir))
        await _init_kernel_matplotlib(km)


# 静态初始化代码（matplotlib 字体配置等，不包含任何动态内容）
_KERNEL_INIT_CODE = """
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import shutil, os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='matplotlib')

# 清除字体缓存以确保字体更新生效
cache_dir = os.path.expanduser("~/.cache/matplotlib")
if os.path.exists(cache_dir):
    shutil.rmtree(cache_dir)

# 重建字体列表
fm._load_fontmanager(try_read_cache=False)

# 配置中文字体 + Symbola 作为 emoji fallback
# Symbola 是矢量字体，支持任意缩放的 emoji 符号
font_candidates = ['Noto Sans CJK SC', 'Noto Sans CJK JP', 'Noto Sans CJK TC']

# 检查 Symbola 字体是否可用
symbola_available = any('Symbola' in f.name for f in fm.fontManager.ttflist)
if symbola_available:
    # 将 Symbola 加入 fallback 列表（用于 emoji）
    font_candidates.append('Symbola')

font_candidates.append('DejaVu Sans')

plt.rcParams['font.sans-serif'] = font_candidates
plt.rcParams['axes.unicode_minus'] = False
"""


async def _init_kernel_matplotlib(km: AsyncKernelManager):
    """初始化内核的 matplotlib 配置
    
    执行静态初始化代码来配置中文字体等。
    工作目录已在 start_kernel(cwd=...) 时设置。
    """
    kc = km.client()
    try:
        # 执行静态初始化代码（不包含任何动态内容）
        kc.execute(_KERNEL_INIT_CODE, silent=True, store_history=False)
        
        # 等待执行完成
        timeout = 10
        while True:
            try:
                msg = await asyncio.wait_for(kc.get_iopub_msg(), timeout=timeout)
                if (
                    msg["msg_type"] == "status"
                    and msg["content"].get("execution_state") == "idle"
                ):
                    break
            except asyncio.TimeoutError:
                break

    except Exception as e:
        print(f"Warning: Failed to initialize matplotlib: {e}")


async def execute_code_in_kernel(
    km: AsyncKernelManager, code: str, timeout: int = 30, silent: bool = False
) -> Dict[str, Any]:
    """在内核中执行代码"""
    await ensure_kernel_running(km)

    kc = km.client()

    try:
        # 执行代码
        kc.execute(code, silent=silent, store_history=not silent)

        outputs = {
            "text": "",
            "images": [],
        }
        plains = []
        execution_count = None
        error = None

        # 等待执行完成
        while True:
            try:
                msg = await asyncio.wait_for(kc.get_iopub_msg(), timeout=timeout)
                msg_type = msg["msg_type"]
                content = msg["content"]

                if msg_type == "execute_input":
                    execution_count = content.get("execution_count")
                elif msg_type == "execute_result":
                    data = content.get("data", {})
                    if isinstance(data, dict):
                        if "text/plain" in data:
                            plains.append(data["text/plain"])
                        if "image/png" in data:
                            outputs["images"].append({"image/png": data["image/png"]})
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if isinstance(data, dict) and "image/png" in data:
                        outputs["images"].append({"image/png": data["image/png"]})
                    elif "text/plain" in data:
                        plains.append(data["text/plain"])
                elif msg_type == "stream":
                    plains.append(content.get("text", ""))
                elif msg_type == "error":
                    error = "\n".join(content.get("traceback", []))
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    # 执行完成
                    break

            except asyncio.TimeoutError:
                error = f"Code execution timed out after {timeout} seconds"
                break

        outputs["text"] = "".join(plains).strip()

        return {
            "success": error is None,
            "execution_count": execution_count,
            "output": outputs,
            "error": error,
        }

    except Exception as e:
        print(f"Error during code execution: {e}")
        return {
            "success": False,
            "execution_count": None,
            "output": {},
            "error": f"Execution error: {str(e)}",
        }


@router.post("/exec", response_model=ExecuteCodeResponse)
async def execute_code(request: ExecuteCodeRequest):
    """执行 IPython 代码"""
    try:
        km = await get_or_create_kernel()

        result = await execute_code_in_kernel(
            km, request.code, timeout=request.timeout, silent=request.silent
        )

        return ExecuteCodeResponse(
            success=result["success"],
            execution_count=result["execution_count"],
            output=result["output"],
            error=result["error"],
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute code: {str(e)}")


@router.get("/kernel/status", response_model=KernelStatusResponse)
async def get_kernel_status():
    """获取内核状态"""
    try:
        global _kernel_manager
        
        if _kernel_manager is None:
            return KernelStatusResponse(
                status="not_started",
                has_kernel=False,
                workspace=str(WORKSPACE_ROOT),
            )

        km = _kernel_manager
        status = "unknown"

        if km.has_kernel:
            if await km.is_alive():
                status = "alive"
            else:
                status = "dead"

        return KernelStatusResponse(
            status=status,
            has_kernel=km.has_kernel,
            workspace=str(WORKSPACE_ROOT),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get kernel status: {str(e)}"
        )


@router.post("/kernel/restart")
async def restart_kernel():
    """重启内核"""
    try:
        global _kernel_manager
        
        if _kernel_manager is not None:
            await _kernel_manager.shutdown_kernel()
            _kernel_manager = None
        
        # 创建新的内核
        await get_or_create_kernel()
        
        return {
            "success": True,
            "message": "Kernel restarted successfully",
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to restart kernel: {str(e)}"
        )


@router.delete("/kernel")
async def shutdown_kernel():
    """关闭内核"""
    try:
        global _kernel_manager
        
        if _kernel_manager is None:
            raise HTTPException(
                status_code=404, detail="Kernel not found"
            )

        await _kernel_manager.shutdown_kernel()
        _kernel_manager = None

        return {
            "success": True,
            "message": "Kernel shutdown successfully",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to shutdown kernel: {str(e)}"
        )
