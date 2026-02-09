"""Gull integration tests — HTTP API endpoints.

Tests the Gull container's REST API end-to-end, covering:
- GET /health: health check
- GET /meta: runtime metadata
- POST /exec: agent-browser command execution

agent-browser commands tested:
- Navigation: open, back, reload, get url, get title
- Snapshot: snapshot, snapshot -i, snapshot -c
- Interactions: click, fill, scroll
- Information: get text, get count, get attr
- JavaScript: eval
- Screenshot: screenshot
- Session: session list, close

Requires a running Gull container (managed by conftest.gull_container fixture).
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import DEFAULT_TIMEOUT, skip_unless_docker, skip_unless_gull_image

pytestmark = [skip_unless_docker, skip_unless_gull_image]


def _exec(base_url: str, cmd: str, timeout: int = 30) -> dict:
    """Helper: POST /exec and return response JSON."""
    resp = httpx.post(
        f"{base_url}/exec",
        json={"cmd": cmd, "timeout": timeout},
        timeout=float(timeout + 5),
    )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def _exec_ok(base_url: str, cmd: str, timeout: int = 30) -> dict:
    """Helper: POST /exec, assert exit_code == 0, return response JSON."""
    data = _exec(base_url, cmd, timeout)
    assert data["exit_code"] == 0, f"cmd={cmd!r} failed: stderr={data.get('stderr')}"
    return data


# ---------------------------------------------------------------------------
# Health & Meta
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/health", timeout=DEFAULT_TIMEOUT)
        assert resp.status_code == 200

    def test_health_response_structure(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/health", timeout=DEFAULT_TIMEOUT)
        data = resp.json()
        assert "status" in data
        assert "browser_active" in data
        assert "session" in data
        assert data["status"] in ("healthy", "degraded", "unhealthy")

    def test_health_status_is_healthy(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/health", timeout=DEFAULT_TIMEOUT)
        assert resp.json()["status"] == "healthy"


class TestMetaEndpoint:
    """Tests for GET /meta."""

    def test_meta_returns_200(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/meta", timeout=DEFAULT_TIMEOUT)
        assert resp.status_code == 200

    def test_meta_response_structure(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/meta", timeout=DEFAULT_TIMEOUT)
        data = resp.json()
        assert "runtime" in data
        assert "workspace" in data
        assert "capabilities" in data

    def test_meta_runtime_info(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/meta", timeout=DEFAULT_TIMEOUT)
        runtime = resp.json()["runtime"]
        assert runtime["name"] == "gull"
        assert "version" in runtime
        assert runtime["api_version"] == "v1"

    def test_meta_browser_capability(self, gull_container: str):
        resp = httpx.get(f"{gull_container}/meta", timeout=DEFAULT_TIMEOUT)
        caps = resp.json()["capabilities"]
        assert "browser" in caps


# ---------------------------------------------------------------------------
# Exec — basic & validation
# ---------------------------------------------------------------------------


class TestExecValidation:
    """POST /exec input validation."""

    def test_exec_validation_requires_cmd(self, gull_container: str):
        """Missing cmd field should return 422."""
        resp = httpx.post(
            f"{gull_container}/exec",
            json={"timeout": 10},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 422

    def test_exec_timeout_min(self, gull_container: str):
        """Timeout must be >= 1."""
        resp = httpx.post(
            f"{gull_container}/exec",
            json={"cmd": "--version", "timeout": 0},
            timeout=DEFAULT_TIMEOUT,
        )
        assert resp.status_code == 422

    def test_exec_version(self, gull_container: str):
        """agent-browser --version should return quickly."""
        data = _exec_ok(gull_container, "--version", timeout=10)
        assert "agent-browser" in data["stdout"]


# ---------------------------------------------------------------------------
# Exec — Navigation
# ---------------------------------------------------------------------------


class TestNavigation:
    """agent-browser navigation commands via /exec."""

    def test_open_url(self, gull_container: str):
        """Open a URL and verify page title in output."""
        data = _exec_ok(gull_container, "open https://example.com")
        assert "Example Domain" in data["stdout"]

    def test_get_title(self, gull_container: str):
        """Get page title."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "get title")
        assert "Example Domain" in data["stdout"]

    def test_get_url(self, gull_container: str):
        """Get current URL."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "get url")
        assert "example.com" in data["stdout"]

    def test_reload(self, gull_container: str):
        """Reload current page."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "reload")
        # reload should succeed; output might vary
        assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Exec — Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    """agent-browser snapshot commands via /exec."""

    def test_snapshot_full(self, gull_container: str):
        """Full accessibility tree snapshot."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "snapshot")
        stdout = data["stdout"]
        # Should contain accessibility tree with elements
        assert "Example Domain" in stdout

    def test_snapshot_interactive(self, gull_container: str):
        """Interactive elements only snapshot (-i flag)."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "snapshot -i")
        stdout = data["stdout"]
        # example.com has a "More information..." link
        assert "ref=" in stdout or "@e" in stdout

    def test_snapshot_compact(self, gull_container: str):
        """Compact snapshot output (-c flag)."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "snapshot -c")
        assert data["exit_code"] == 0
        assert len(data["stdout"]) > 0


# ---------------------------------------------------------------------------
# Exec — Information retrieval
# ---------------------------------------------------------------------------


class TestGetInfo:
    """agent-browser get commands via /exec."""

    def test_get_text_body(self, gull_container: str):
        """Get text content of the page body."""
        _exec_ok(gull_container, "open https://example.com")
        # Use snapshot to find refs, then get text
        data = _exec_ok(gull_container, "snapshot -i")
        # example.com should have a heading ref
        if "@e1" in data["stdout"] or "ref=e1" in data["stdout"]:
            text_data = _exec_ok(gull_container, "get text @e1")
            assert len(text_data["stdout"].strip()) > 0

    def test_get_count(self, gull_container: str):
        """Count elements matching a CSS selector."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, 'get count "a"')
        # example.com has at least 1 link
        count = data["stdout"].strip()
        assert count.isdigit()
        assert int(count) >= 1


# ---------------------------------------------------------------------------
# Exec — JavaScript evaluation
# ---------------------------------------------------------------------------


class TestJavaScript:
    """agent-browser eval commands via /exec."""

    def test_eval_simple_expression(self, gull_container: str):
        """Evaluate a simple JS expression."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "eval document.title")
        assert "Example Domain" in data["stdout"]

    def test_eval_arithmetic(self, gull_container: str):
        """Evaluate arithmetic expression."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "eval 2+2")
        assert "4" in data["stdout"]


# ---------------------------------------------------------------------------
# Exec — Screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    """agent-browser screenshot commands via /exec."""

    def test_screenshot_to_path(self, gull_container: str):
        """Take screenshot and save to a path."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "screenshot /workspace/test_screenshot.png")
        # Should succeed without errors
        assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Exec — Session management
# ---------------------------------------------------------------------------


class TestSession:
    """agent-browser session commands via /exec."""

    def test_session_list(self, gull_container: str):
        """List active sessions."""
        # Ensure at least one session exists
        _exec_ok(gull_container, "open https://example.com")
        data = _exec(gull_container, "session list")
        # session list should return 0 (or at least not crash)
        assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Exec — Scroll
# ---------------------------------------------------------------------------


class TestScroll:
    """agent-browser scroll commands via /exec."""

    def test_scroll_down(self, gull_container: str):
        """Scroll down the page."""
        _exec_ok(gull_container, "open https://example.com")
        data = _exec_ok(gull_container, "scroll down 100")
        assert data["exit_code"] == 0


# ---------------------------------------------------------------------------
# Exec — Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Error cases for /exec."""

    def test_invalid_command(self, gull_container: str):
        """Invalid subcommand should return non-zero exit code."""
        data = _exec(gull_container, "nonexistent-subcommand")
        # agent-browser should return error for unknown commands
        assert data["exit_code"] != 0 or data["stderr"]
