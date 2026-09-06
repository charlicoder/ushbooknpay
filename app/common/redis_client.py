"""
app/common/redis_client.py
──────────────────────────
Async Redis client singleton using redis.asyncio.

Provides:
- Lazy-initialised connection pool
- FastAPI dependency
- Typed helper for common operations
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import ConnectionPool, Redis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None  # type: ignore[type-arg]


def get_redis_pool() -> ConnectionPool:
    """Return the shared connection pool (created lazily)."""
    global _redis_pool
    if _redis_pool is None:
        settings = get_settings()
        _redis_pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT,
            decode_responses=True,
            encoding="utf-8",
            health_check_interval=30,
        )
        logger.info(
            "redis_pool_created",
            max_connections=settings.REDIS_MAX_CONNECTIONS,
        )
    return _redis_pool


def get_redis() -> Redis:  # type: ignore[type-arg]
    """Return the shared Redis client (created lazily)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(connection_pool=get_redis_pool())
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection pool. Call during app shutdown."""
    global _redis_pool, _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None
    logger.info("redis_pool_closed")


# FastAPI dependency
async def get_redis_dep() -> Redis:  # type: ignore[type-arg]
    """
    FastAPI dependency that returns the shared Redis client.

    Usage::

        @router.get("/")
        async def handler(redis: Redis = Depends(get_redis_dep)):
            ...
    """
    return get_redis()
