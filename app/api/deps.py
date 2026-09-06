"""
app/api/deps.py
────────────────
FastAPI dependency factory functions.

Centralises all dependency injection including:
- Database session
- Redis client
- HTTP client (for ushauth)
- Booking/Payment services
- ushauth client
- Current user extraction
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.application.services import BookingService
from app.common.redis_client import get_redis_dep
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.security import TokenPayload, require_app_token, require_authenticated_user
from app.integrations.ushauth_client import USHAuthClient


# ── Shared HTTP client ─────────────────────────────────────────────────────
# Module-level singleton — httpx.AsyncClient is thread-safe and reusable
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared httpx AsyncClient."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _http_client


async def close_http_client() -> None:
    """Close the shared httpx client. Call during app shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# ── Dependency type aliases ─────────────────────────────────────────────────
DBSession = Annotated[AsyncSession, Depends(get_db_session)]
RedisClient = Annotated[Any, Depends(get_redis_dep)]
CurrentUser = Annotated[TokenPayload, Depends(require_authenticated_user)]
RequireAppToken = Annotated[None, Depends(require_app_token)]
AppSettings = Annotated[Settings, Depends(get_settings)]


# ── USHAuth client dependency ───────────────────────────────────────────────

async def get_ushauth_client(
    redis: RedisClient,
    settings: AppSettings,
) -> USHAuthClient:
    """Provide a USHAuthClient per request."""
    return USHAuthClient(
        http_client=get_http_client(),
        redis_client=redis,
        settings=settings,
    )


USHAuthDep = Annotated[USHAuthClient, Depends(get_ushauth_client)]


# ── Booking service dependency ──────────────────────────────────────────────

async def get_booking_service(
    session: DBSession,
    settings: AppSettings,
) -> BookingService:
    """Provide a BookingService per request."""
    return BookingService(session=session, settings=settings)


BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]
