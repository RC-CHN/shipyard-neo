"""Bay FastAPI application entry point."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import close_db, init_db
from app.errors import BayError
from app.services.gc.lifecycle import init_gc_scheduler, shutdown_gc_scheduler
from app.services.http import http_client_manager

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("bay.startup", version="0.1.0")
    await init_db()

    # Initialize HTTP client with connection pooling
    await http_client_manager.startup()

    # Initialize and start GC scheduler
    await init_gc_scheduler()

    yield

    # Shutdown
    logger.info("bay.shutdown")

    # Stop GC scheduler
    await shutdown_gc_scheduler()

    # Close HTTP client
    await http_client_manager.shutdown()

    await close_db()


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Bay",
        description="Orchestration layer for Ship containers",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Request ID middleware
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """Add request ID to all requests."""
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    # Error handler
    @app.exception_handler(BayError)
    async def bay_error_handler(request: Request, exc: BayError):
        """Handle Bay errors with consistent format."""
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(request_id),
        )

    # Health check
    @app.get("/health")
    async def health() -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    # Import and register API routers
    from app.api.v1 import router as v1_router

    app.include_router(v1_router, prefix="/v1")

    return app


# Create default app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=True,
    )
