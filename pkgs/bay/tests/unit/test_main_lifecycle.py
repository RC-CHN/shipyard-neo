"""Unit tests for Bay app lifecycle wiring in app.main."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app import main as main_module


@pytest.mark.asyncio
async def test_lifespan_wires_startup_and_shutdown_in_order(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []

    async def _record(name: str):
        events.append(name)

    async def init_db() -> None:
        await _record("init_db")

    async def close_db() -> None:
        await _record("close_db")

    async def init_gc_scheduler() -> None:
        await _record("init_gc_scheduler")

    async def shutdown_gc_scheduler() -> None:
        await _record("shutdown_gc_scheduler")

    async def init_browser_learning_scheduler() -> None:
        await _record("init_browser_learning_scheduler")

    async def shutdown_browser_learning_scheduler() -> None:
        await _record("shutdown_browser_learning_scheduler")

    @asynccontextmanager
    async def fake_get_async_session():
        yield object()

    async def fake_auto_provision(db, settings):  # noqa: ANN001, ANN202
        await _record("api_key_auto_provision")
        return {}

    class FakeHTTPClientManager:
        async def startup(self) -> None:
            await _record("http_startup")

        async def shutdown(self) -> None:
            await _record("http_shutdown")

    import app.db.session as db_session_module
    from app.services.api_key import ApiKeyService

    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(main_module, "close_db", close_db)
    monkeypatch.setattr(main_module, "init_gc_scheduler", init_gc_scheduler)
    monkeypatch.setattr(main_module, "shutdown_gc_scheduler", shutdown_gc_scheduler)
    monkeypatch.setattr(
        main_module,
        "init_browser_learning_scheduler",
        init_browser_learning_scheduler,
    )
    monkeypatch.setattr(
        main_module,
        "shutdown_browser_learning_scheduler",
        shutdown_browser_learning_scheduler,
    )
    monkeypatch.setattr(main_module, "http_client_manager", FakeHTTPClientManager())
    monkeypatch.setattr(db_session_module, "get_async_session", fake_get_async_session)
    monkeypatch.setattr(ApiKeyService, "auto_provision", staticmethod(fake_auto_provision))

    app = SimpleNamespace(state=SimpleNamespace())
    async with main_module.lifespan(app):
        events.append("inside")

    assert app.state.api_key_hashes == {}
    assert events == [
        "init_db",
        "api_key_auto_provision",
        "http_startup",
        "init_gc_scheduler",
        "init_browser_learning_scheduler",
        "inside",
        "shutdown_gc_scheduler",
        "shutdown_browser_learning_scheduler",
        "http_shutdown",
        "close_db",
    ]


@pytest.mark.asyncio
async def test_request_id_middleware_sets_response_header():
    app = main_module.create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        explicit = await client.get("/health", headers={"X-Request-Id": "req-fixed"})
        assert explicit.status_code == 200
        assert explicit.headers["X-Request-Id"] == "req-fixed"

        generated = await client.get("/health")
        assert generated.status_code == 200
        assert generated.headers.get("X-Request-Id")
