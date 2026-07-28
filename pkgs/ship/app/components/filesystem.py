import os
from urllib.parse import quote
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse as FastAPIFileResponse
from pydantic import BaseModel
from ..workspace import resolve_path

router = APIRouter()


def _content_disposition(filename: str) -> str:
    """Build an ASCII fallback and RFC 5987 UTF-8 attachment filename."""
    ascii_fallback = "".join(
        character
        if 32 <= ord(character) < 127 and character not in {'"', "\\", ";"}
        else "_"
        for character in filename
    ).strip()
    if not ascii_fallback:
        ascii_fallback = "download"
    encoded_filename = quote(filename, safe="!#$&+-.^_`|~")
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )


# 定义请求和响应模型
class CreateFileRequest(BaseModel):
    path: str
    content: str = ""
    mode: int = 0o644


class ReadFileRequest(BaseModel):
    path: str
    encoding: str = "utf-8"
    offset: Optional[int] = None  # 起始行号（1-based），None 表示从头开始
    limit: Optional[int] = None  # 最大读取行数，None 表示读取所有行


class WriteFileRequest(BaseModel):
    path: str
    content: str
    mode: str = "w"  # "w" for write, "a" for append
    encoding: str = "utf-8"


class EditFileRequest(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False
    encoding: str = "utf-8"


class DeleteFileRequest(BaseModel):
    path: str


class ListDirRequest(BaseModel):
    path: str = "."
    show_hidden: bool = False


class FileInfo(BaseModel):
    name: str
    path: str
    is_file: bool
    is_dir: bool
    size: Optional[int] = None
    modified_time: Optional[float] = None


class ListDirResponse(BaseModel):
    files: List[FileInfo]
    current_path: str


class FileResponse(BaseModel):
    content: str
    path: str
    size: int


@router.post("/create_file")
async def create_file(request: CreateFileRequest):
    """创建文件"""
    try:
        file_path = resolve_path(request.path)

        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建文件
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(request.content)

        # 设置文件权限
        os.chmod(file_path, request.mode)

        return {
            "success": True,
            "message": f"File created: {request.path}",
            "path": str(file_path.absolute()),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create file: {str(e)}")


@router.post("/read_file", response_model=FileResponse)
async def read_file(request: ReadFileRequest):
    """读取文件内容"""
    try:
        file_path = resolve_path(request.path)

        if not file_path.exists():
            raise HTTPException(
                status_code=404, detail=f"File not found: {request.path}"
            )

        if not file_path.is_file():
            raise HTTPException(
                status_code=400, detail=f"Path is not a file: {request.path}"
            )

        async with aiofiles.open(file_path, "r", encoding=request.encoding) as f:
            lines = await f.readlines()
            offset = request.offset if request.offset is not None else 1
            limit = request.limit if request.limit is not None else len(lines)
            start_index = max(0, offset - 1) if offset > 0 else 0
            if start_index >= len(lines):
                content = ""
            else:
                end_index = start_index + limit
                content = "".join(lines[start_index:end_index])

        stat = file_path.stat()
        return FileResponse(
            content=content, path=str(file_path.absolute()), size=stat.st_size
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {str(e)}")


@router.post("/write_file")
async def write_file(request: WriteFileRequest):
    """写入文件内容"""
    try:
        file_path = resolve_path(request.path)

        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # 确保模式是合法的
        mode = "w" if request.mode == "w" else "a"

        async with aiofiles.open(file_path, mode, encoding=request.encoding) as f:
            await f.write(request.content)

        stat = file_path.stat()
        return {
            "success": True,
            "message": f"File written: {request.path}",
            "path": str(file_path.absolute()),
            "size": stat.st_size,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")


@router.post("/edit_file")
async def edit_file(request: EditFileRequest):
    """编辑文件内容 - 替换指定的字符串"""
    try:
        file_path = resolve_path(request.path)

        if not file_path.exists():
            raise HTTPException(
                status_code=404, detail=f"File not found: {request.path}"
            )
        if not file_path.is_file():
            raise HTTPException(
                status_code=400, detail=f"Path is not a file: {request.path}"
            )
        if request.old_string == request.new_string:
            raise HTTPException(
                status_code=400, detail="'old_string' and 'new_string' must differ"
            )

        async with aiofiles.open(file_path, "r", encoding=request.encoding) as f:
            content = await f.read()

        # 检查要替换的字符串是否存在
        count = content.count(request.old_string)
        if count == 0:
            raise HTTPException(
                status_code=400, detail="'old_string' not found in file"
            )

        # 检查是否需要替换所有出现的字符串
        if count > 1 and not request.replace_all:
            raise HTTPException(
                status_code=400,
                detail=f"'old_string' appears {count} times in file; set replace_all=true to replace all occurrences",
            )

        # 执行替换
        if request.replace_all:
            updated_content = content.replace(request.old_string, request.new_string)
            replacements = count
        else:
            updated_content = content.replace(request.old_string, request.new_string, 1)
            replacements = 1

        # 写入更新后的内容
        async with aiofiles.open(file_path, "w", encoding=request.encoding) as f:
            await f.write(updated_content)

        stat = file_path.stat()
        return {
            "success": True,
            "message": f"File edited: {request.path}",
            "path": str(file_path.absolute()),
            "replacements": replacements,
            "size": stat.st_size,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to edit file: {str(e)}")


@router.post("/delete_file")
async def delete_file(request: DeleteFileRequest):
    """删除文件或目录"""
    try:
        file_path = resolve_path(request.path)

        if not file_path.exists():
            raise HTTPException(
                status_code=404, detail=f"Path not found: {request.path}"
            )

        if file_path.is_file():
            file_path.unlink()
            return {"success": True, "message": f"File deleted: {request.path}"}
        elif file_path.is_dir():
            # 递归删除目录
            import shutil

            shutil.rmtree(file_path)
            return {"success": True, "message": f"Directory deleted: {request.path}"}
        else:
            raise HTTPException(
                status_code=400, detail=f"Unknown path type: {request.path}"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")


@router.post("/list_dir", response_model=ListDirResponse)
async def list_directory(request: ListDirRequest):
    """列出目录内容"""
    try:
        dir_path = resolve_path(request.path)

        if not dir_path.exists():
            raise HTTPException(
                status_code=404, detail=f"Directory not found: {request.path}"
            )

        if not dir_path.is_dir():
            raise HTTPException(
                status_code=400, detail=f"Path is not a directory: {request.path}"
            )

        files = []
        for item in dir_path.iterdir():
            # 跳过隐藏文件（除非明确要求显示）
            if not request.show_hidden and item.name.startswith("."):
                continue

            try:
                resolved_item = resolve_path(str(item))
                stat = resolved_item.stat()
                file_info = FileInfo(
                    name=item.name,
                    path=str(resolved_item),
                    is_file=resolved_item.is_file(),
                    is_dir=resolved_item.is_dir(),
                    size=stat.st_size if resolved_item.is_file() else None,
                    modified_time=stat.st_mtime,
                )
                files.append(file_info)
            except (HTTPException, OSError, PermissionError):
                # 跳过无法访问的文件
                continue

        # 排序：目录在前，然后按名称排序
        files.sort(key=lambda x: (not x.is_dir, x.name.lower()))

        return ListDirResponse(files=files, current_path=str(dir_path.absolute()))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list directory: {str(e)}"
        )


# ========== Upload / Download (统一到 filesystem) ==========


class UploadResponse(BaseModel):
    success: bool
    message: str
    file_path: Optional[str] = None
    size: Optional[int] = None
    error: Optional[str] = None


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    file_path: str = Form(...),
):
    """上传文件到 workspace 目录"""
    try:
        # 解析并验证目标路径
        target_path = resolve_path(file_path)

        # 确保父目录存在
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # 读取文件内容并写入目标路径
        content = await file.read()

        async with aiofiles.open(target_path, "wb") as f:
            await f.write(content)

        return UploadResponse(
            success=True,
            message="File uploaded successfully",
            file_path=str(target_path),
            size=len(content),
        )

    except HTTPException:
        # 重新抛出HTTP异常（如路径验证失败）
        raise
    except Exception as e:
        return UploadResponse(success=False, message="File upload failed", error=str(e))


@router.get("/download")
async def download_file(file_path: str):
    """从 workspace 目录下载文件"""
    try:
        # 解析并验证路径
        target_path = resolve_path(file_path)

        # 检查文件是否存在
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="File not found")

        # 检查是否是文件（不是目录）
        if not target_path.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")

        # 返回文件
        return FastAPIFileResponse(
            path=str(target_path),
            media_type="application/octet-stream",
            headers={"Content-Disposition": _content_disposition(target_path.name)},
        )

    except HTTPException:
        # 重新抛出HTTP异常
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File download failed: {str(e)}")
