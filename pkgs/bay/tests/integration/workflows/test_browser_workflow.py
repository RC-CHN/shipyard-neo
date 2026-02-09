"""Browser integration workflow (Phase 2).

Goal: Validate Ship + Gull multi-container sandbox works end-to-end:
1) Ship: create a tiny HTTP server serving a test HTML page from /workspace
2) Gull: open the page via container-network DNS (http://ship:9000)
3) Gull: screenshot to /workspace (shared Cargo volume)
4) Ship: download/read the screenshot and parse PNG dimensions with Python

This test is placed under workflows/ so it is treated as serial/exclusive via
[`SERIAL_GROUPS["workflows"]`](pkgs/bay/tests/integration/conftest.py:72).

Note: This test is Docker-only for now.
"""

from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from ..conftest import (
    AUTH_HEADERS,
    BAY_BASE_URL,
    DEFAULT_TIMEOUT,
    EXEC_TIMEOUT,
    create_sandbox,
    e2e_skipif_marks,
)

pytestmark = e2e_skipif_marks


def _docker_image_exists(image: str) -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        return False


async def _wait_until_http_ok(
    client: httpx.AsyncClient,
    sandbox_id: str,
    url: str,
    *,
    max_attempts: int = 30,
) -> None:
    """Poll from inside Ship (shell) until the URL returns 200."""
    for i in range(max_attempts):
        r = await client.post(
            f"/v1/sandboxes/{sandbox_id}/shell/exec",
            json={
                "command": (
                    "python -c \"import urllib.request; "
                    "import sys; "
                    "u=sys.argv[1]; "
                    "print(urllib.request.urlopen(u, timeout=2).status)\" "
                    f"{url}"
                ),
                "timeout": 10,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        assert r.status_code == 200
        out = (r.json().get("output") or "").strip()
        if "200" in out:
            return
        await asyncio.sleep(0.5)

    raise AssertionError(f"HTTP server not ready after {max_attempts} attempts: {url}")


async def test_browser_screenshot_download_and_python_parse_png_dimensions():
    """Ship+Gull E2E: open page -> screenshot -> download -> python parse."""

    # Docker-only for now (k8s images/registry + scheduling would need separate harness)
    from ..conftest import E2E_DRIVER_TYPE

    if E2E_DRIVER_TYPE != "docker":
        pytest.skip("browser workflow e2e is docker-only for now")

    if not _docker_image_exists("gull:latest"):
        pytest.skip("gull:latest image not available (build pkgs/gull/Dockerfile)")

    profile_id = "browser-python"

    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
        async with create_sandbox(client, profile=profile_id) as sandbox:
            sandbox_id = sandbox["id"]

            # 1) Write a tiny HTML page into /workspace
            html = """<!doctype html>
<html>
  <head><meta charset='utf-8'><title>Bay Browser E2E</title></head>
  <body>
    <h1 id='hello'>hello-from-ship</h1>
  </body>
</html>
"""
            w = await client.put(
                f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                json={"path": "www/index.html", "content": html},
                timeout=EXEC_TIMEOUT,
            )
            assert w.status_code == 200, w.text

            # 2) Start HTTP server in Ship (background)
            # Note: Ship shell endpoint does not expose "background"; we use '&' to detach.
            r = await client.post(
                f"/v1/sandboxes/{sandbox_id}/shell/exec",
                json={
                    "command": "python -m http.server 9000 --directory /workspace/www >/workspace/http.log 2>&1 & echo started",
                    "timeout": 10,
                },
                timeout=DEFAULT_TIMEOUT,
            )
            assert r.status_code == 200
            assert r.json().get("success") is True

            # 3) Wait until server is ready (from inside Ship)
            await _wait_until_http_ok(
                client,
                sandbox_id,
                "http://127.0.0.1:9000/index.html",
            )

            # 4) Gull: open page via container DNS (ship hostname)
            b1 = await client.post(
                f"/v1/capabilities/{sandbox_id}/browser/exec",
                json={"cmd": "open http://ship:9000/index.html", "timeout": 60},
                timeout=EXEC_TIMEOUT,
            )
            assert b1.status_code == 200, b1.text
            assert b1.json().get("success") is True, b1.json()

            # 5) Gull: screenshot into shared /workspace
            screenshot_path = "/workspace/browser_e2e.png"
            b2 = await client.post(
                f"/v1/capabilities/{sandbox_id}/browser/exec",
                json={"cmd": f"screenshot {screenshot_path}", "timeout": 120},
                timeout=EXEC_TIMEOUT,
            )
            assert b2.status_code == 200, b2.text
            assert b2.json().get("success") is True, b2.json()

            # 6) Ship: download screenshot
            d = await client.get(
                f"/v1/sandboxes/{sandbox_id}/filesystem/download",
                params={"path": "browser_e2e.png"},
                timeout=EXEC_TIMEOUT,
            )
            assert d.status_code == 200
            assert d.content[:8] == b"\x89PNG\r\n\x1a\n"

            # 7) Ship: parse PNG dimensions using python (no pillow dependency)
            py = await client.post(
                f"/v1/sandboxes/{sandbox_id}/python/exec",
                json={
                    "code": """
import struct
with open('browser_e2e.png','rb') as f:
    data = f.read(24)
assert data[:8] == b"\x89PNG\r\n\x1a\n"
w, h = struct.unpack('>II', data[16:24])
print(f"png_size={w}x{h}")
""",
                    "timeout": 30,
                },
                timeout=EXEC_TIMEOUT,
            )
            assert py.status_code == 200
            assert py.json().get("success") is True, py.json()
            assert "png_size=" in (py.json().get("output") or "")
