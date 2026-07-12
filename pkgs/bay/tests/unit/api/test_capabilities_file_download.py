"""Unit tests for filesystem download response headers."""

from __future__ import annotations

from fastapi.responses import Response

from app.api.v1.capabilities import _attachment_content_disposition


def test_content_disposition_encodes_unicode_filename() -> None:
    header = _attachment_content_disposition("中文测试文件.txt")

    assert header == (
        "attachment; filename*=UTF-8''"
        "%E4%B8%AD%E6%96%87%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6.txt"
    )
    assert header.isascii()
    assert Response(headers={"Content-Disposition": header}).headers[
        "content-disposition"
    ] == header


def test_content_disposition_escapes_header_metacharacters() -> None:
    header = _attachment_content_disposition('report "final".txt')

    assert header == "attachment; filename*=UTF-8''report%20%22final%22.txt"
    assert header.isascii()
