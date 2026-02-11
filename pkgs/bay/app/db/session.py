"""Database session management using SQLModel async."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

# Import all models to ensure they are registered with SQLModel metadata
# This is required for relationship resolution at runtime
import app.models  # noqa: F401
from app.config import get_settings

logger = structlog.get_logger()

# Lazy initialization - engine created on first use
_engine = None
_async_session_factory = None


def _get_engine():
    """Get or create the async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database.url,
            echo=settings.database.echo,
            future=True,
        )
    return _engine


def _get_session_factory():
    """Get or create the async session factory."""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


def _sqlite_default_for_type(col) -> str:
    """Determine a safe DEFAULT clause for an ALTER TABLE ADD COLUMN in SQLite.

    SQLite requires a DEFAULT for NOT NULL columns added via ALTER TABLE.
    This function derives a sensible default from the column's Python-side
    default or its type.
    """
    from sqlalchemy import Boolean, DateTime, Float, Integer, String

    # 1. Explicit server_default
    if col.server_default is not None:
        return f" DEFAULT {col.server_default.arg}"

    # 2. Nullable columns can simply default to NULL
    if col.nullable:
        return " DEFAULT NULL"

    # 3. NOT NULL column — derive default from Python-side default or type
    if col.default is not None and col.default.arg is not None:
        py_default = col.default.arg
        # callable defaults (e.g. default_factory) can't be expressed in DDL;
        # fall back to type-based defaults below.
        if not callable(py_default):
            if isinstance(py_default, bool):
                return f" DEFAULT {1 if py_default else 0}"
            if isinstance(py_default, (int, float)):
                return f" DEFAULT {py_default}"
            if isinstance(py_default, str):
                escaped = py_default.replace("'", "''")
                return f" DEFAULT '{escaped}'"

    # 4. Type-based fallback for NOT NULL columns without usable defaults
    col_type = type(col.type)
    if issubclass(col_type, Boolean):
        return " DEFAULT 0"
    if issubclass(col_type, (Integer, Float)):
        return " DEFAULT 0"
    if issubclass(col_type, String):
        return " DEFAULT ''"
    if issubclass(col_type, DateTime):
        return " DEFAULT '1970-01-01 00:00:00'"

    # 5. Last resort — make it nullable in DDL to avoid crash
    return " DEFAULT NULL"


def _auto_migrate_sync(conn) -> None:
    """Auto-migrate existing tables to add missing columns.

    SQLModel's create_all only creates new tables — it does NOT alter
    existing tables to add new columns.  This helper inspects the live
    schema and issues ALTER TABLE … ADD COLUMN for any column present
    in the model but absent from the database.

    Only supports column *additions* (safe, non-destructive).
    Does NOT handle column renames, type changes, or removals.
    """
    inspector = inspect(conn)
    existing_tables = inspector.get_table_names()

    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            # Table will be created by create_all — skip
            continue

        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}

        for col in table.columns:
            if col.name in existing_cols:
                continue

            # Build column type DDL string
            col_type = col.type.compile(dialect=conn.dialect)
            default_clause = _sqlite_default_for_type(col)

            # SQLite ALTER TABLE ADD COLUMN with NOT NULL requires a DEFAULT.
            # If we have a default, keep NOT NULL; otherwise make it nullable
            # in DDL to avoid SQLite errors.
            if not col.nullable and default_clause.strip().upper().startswith("DEFAULT NULL"):
                nullable = "NULL"
            else:
                nullable = "NULL" if col.nullable else "NOT NULL"

            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type} {nullable}{default_clause}"
            logger.info(
                "db.auto_migrate.add_column",
                table=table.name,
                column=col.name,
                ddl=ddl,
            )
            conn.execute(text(ddl))


async def init_db() -> None:
    """Initialize database tables.

    Note: In production, use Alembic migrations instead.
    This is for development/testing convenience.
    """
    engine = _get_engine()
    async with engine.begin() as conn:
        # Auto-migrate existing tables first (add missing columns)
        await conn.run_sync(_auto_migrate_sync)
        # Then create any entirely new tables
        await conn.run_sync(SQLModel.metadata.create_all)


async def close_db() -> None:
    """Close database connection."""
    global _engine, _async_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Get async database session as context manager.

    Usage:
        async with get_async_session() as session:
            result = await session.exec(select(Model))
    """
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session_dependency() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for getting database session.

    Usage:
        @app.get("/items")
        async def get_items(session: AsyncSession = Depends(get_session_dependency)):
            ...
    """
    async with get_async_session() as session:
        yield session
