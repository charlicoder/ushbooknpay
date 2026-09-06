"""
app/core/database.py
────────────────────
Async SQLAlchemy 2.x database setup.

Provides:
- Async engine with connection pool tuned for production
- Async session factory
- FastAPI dependency for session injection
- Base declarative class shared across all ORM models
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Naming convention for Alembic auto-generation ────────────────────────────
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Shared SQLAlchemy declarative base with async attribute support."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    __mapper_args__ = {"eager_defaults": True}


def create_engine() -> Any:
    """Create and return the async SQLAlchemy engine."""
    settings = get_settings()

    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_timeout=settings.DATABASE_POOL_TIMEOUT,
        pool_pre_ping=True,           # Detect stale connections
        pool_recycle=1800,            # Recycle connections every 30 minutes
        echo=settings.APP_DEBUG,      # Log SQL only in debug mode
        future=True,
    )

    logger.info(
        "database_engine_created",
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
    )
    return engine


# Module-level singletons — initialised once at startup
_engine: Any = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> Any:
    """Return the module-level engine (created lazily on first call)."""
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory (created lazily)."""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # Avoid implicit lazy loads after commit
            autoflush=False,
            autocommit=False,
        )
    return _async_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a database session per request.

    Automatically commits on success and rolls back on exception.

    Usage::

        @router.post("/")
        async def create_booking(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Gracefully close the connection pool. Call during app shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        logger.info("database_engine_disposed")
        _engine = None
