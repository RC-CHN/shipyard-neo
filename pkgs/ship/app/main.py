from fastapi import FastAPI
from contextlib import asynccontextmanager
from .components.filesystem import router as fs_router
from .components.ipython import router as ipython_router, get_or_create_kernel
from .components.shell import router as shell_router
from .components.term import router as term_router
from .workspace import WORKSPACE_ROOT
import logging
import os
import tomli
from pathlib import Path

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理
    
    启动时预热 Jupyter Kernel，避免首次请求时的冷启动延迟。
    """
    logger.info("Starting Ship container...")
    
    # 预热 Jupyter Kernel（在后台启动）
    # 这可以节省首次 /ipython/exec 请求时约 3-5 秒的内核启动时间
    try:
        logger.info("Pre-warming Jupyter kernel...")
        await get_or_create_kernel()
        logger.info("Jupyter kernel pre-warmed successfully")
    except Exception as e:
        # 预热失败不阻止服务启动，首次请求时会重试
        logger.warning(f"Failed to pre-warm Jupyter kernel: {e}")
    
    yield
    logger.info("Ship container shutting down")


def get_version() -> str:
    """Get version from pyproject.toml."""
    try:
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomli.load(f)
        return data.get("project", {}).get("version", "unknown")
    except Exception:
        return "unknown"


# Determine runtime version from pyproject.toml
RUNTIME_VERSION = get_version()

app = FastAPI(
    title="Ship API",
    description="A containerized execution environment with filesystem, IPython, and shell capabilities",
    version=RUNTIME_VERSION,
    lifespan=lifespan,
)

# Include component routers
app.include_router(fs_router, prefix="/fs", tags=["filesystem"])
app.include_router(ipython_router, prefix="/ipython", tags=["ipython"])
app.include_router(shell_router, prefix="/shell", tags=["shell"])
app.include_router(term_router, prefix="/term", tags=["terminal"])


@app.get("/")
async def root():
    return {"message": "Ship API is running"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


def get_build_info() -> dict:
    """Best-effort build/image metadata for diagnostics."""
    return {
        "image": os.environ.get("SHIP_IMAGE", "ship:default"),
        "image_digest": os.environ.get("SHIP_IMAGE_DIGEST"),
        "git_sha": os.environ.get("GIT_SHA"),
    }


@app.get("/meta")
async def get_meta():
    """Runtime self-description endpoint.

    This endpoint is used by Bay to validate runtime version and capabilities.
    """
    return {
        "runtime": {
            "name": "ship",
            "version": get_version(),
            "api_version": "v1",
            "build": get_build_info(),
        },
        "workspace": {
            "mount_path": str(WORKSPACE_ROOT),
        },
        "capabilities": {
            "filesystem": {
                "operations": ["create", "read", "write", "edit", "delete", "list", "upload", "download"],
                "path_mode": "relative_to_mount",
                "endpoints": {
                    "create": "/fs/create_file",
                    "read": "/fs/read_file",
                    "write": "/fs/write_file",
                    "edit": "/fs/edit_file",
                    "delete": "/fs/delete_file",
                    "list": "/fs/list_dir",
                    "upload": "/fs/upload",
                    "download": "/fs/download",
                },
            },
            "shell": {
                "operations": ["exec", "processes"],
                "endpoints": {
                    "exec": "/shell/exec",
                    "processes": "/shell/processes",
                },
            },
            "python": {
                "operations": ["exec"],
                "engine": "ipython",
                "endpoints": {
                    "exec": "/ipython/exec",
                },
            },
            "terminal": {
                "operations": ["ws"],
                "protocol": "websocket",
                "endpoints": {
                    "ws": "/term/ws",
                },
            },
        },
    }




@app.get("/stat")
async def get_stat():
    """Get service statistics and version information"""
    return {
        "service": "ship",
        "version": get_version(),
        "status": "running",
        "author": "AstrBot Team",
    }
