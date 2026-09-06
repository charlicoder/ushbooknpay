"""
app/main.py
────────────
FastAPI application factory with lifespan management.

Responsibilities:
- Application creation
- Middleware registration (in correct order)
- Lifespan startup/shutdown hooks
  - Configure structured logging
  - Initialise database engine and Redis pool
  - Start background outbox publisher
  - Graceful teardown
- Exception handler registration
- API router mounting
- OpenAPI customisation
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.booking.infrastructure.models import Booking, BookingStatusHistory, TemporaryHold  # noqa: F401
from app.promotions.infrastructure.models import LoyaltyTracker, LoyaltyReward  # noqa: F401
from app.common.redis_client import close_redis, get_redis
from app.core.config import get_settings
from app.core.database import Base, dispose_engine, get_engine, get_session_factory
from app.core.exceptions import USHBaseError
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    IdempotencyMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecureHeadersMiddleware,
)
from app.events.sqs_client import SQSClient, get_sqs_client
from app.payment.infrastructure.models import Payment, PaymentStatusHistory  # noqa: F401
from app.voucher.infrastructure.models import GiftVoucher  # noqa: F401

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.

    Startup:
        1. Configure structured logging
        2. Verify settings (will raise if missing required fields)
        3. Initialise DB engine and session factory
        4. Initialise Redis connection pool
        5. Initialize SQS publisher client

    Shutdown:
        1. Close Redis pool
        2. Dispose database engine
        3. Close HTTP clients
    """
    settings = get_settings()

    # ── Startup ───────────────────────────────────────────────────────────
    configure_logging(
        log_level=settings.LOG_LEVEL,
        is_development=settings.is_development,
    )

    logger.info(
        "service_starting",
        name=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
    )

    # Initialise DB and ensure tables exist
    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database_schema_initialized")
    except Exception as exc:
        logger.warning("database_schema_init_warning", error=str(exc))

    get_session_factory()

    # Initialise Redis (validates connection)
    get_redis()

    # Initialise SQS client
    get_sqs_client()

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("service_shutting_down")

    await close_redis()
    await dispose_engine()

    # Close shared httpx client
    from app.api.deps import close_http_client
    await close_http_client()

    logger.info("service_stopped")


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="USHSPA Booking & Payment Service",
        description=(
            "Production-ready microservice for appointment booking and payment "
            "processing for the USHSPA mobile application."
        ),
        version=settings.APP_VERSION,
        docs_url="/api/docs/" if not settings.is_production else None,
        redoc_url="/api/redoc/" if not settings.is_production else None,
        openapi_url="/api/schema/" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Middleware (applied in REVERSE order of registration) ─────────────
    # Outermost → innermost: RequestID → SecureHeaders → RateLimit → Idempotency

    app.add_middleware(
        IdempotencyMiddleware,
        redis_client=get_redis(),
        ttl_seconds=settings.IDEMPOTENCY_TTL_SECONDS,
    )
    app.add_middleware(
        RateLimitMiddleware,
        redis_client=get_redis(),
        limit=settings.RATE_LIMIT_REQUESTS,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    app.add_middleware(
        SecureHeadersMiddleware,
        is_production=settings.is_production,
    )
    app.add_middleware(RequestIDMiddleware)

    # ── Exception handlers ─────────────────────────────────────────────────
    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {
                    "code": getattr(exc, "code", "HTTP_ERROR"),
                    "message": exc.detail if isinstance(exc.detail, str) else "HTTP Exception",
                    "detail": exc.detail if not isinstance(exc.detail, str) else None,
                },
            },
            headers=exc.headers,
        )

    @app.exception_handler(USHBaseError)
    async def domain_exception_handler(
        request: Request, exc: USHBaseError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail if settings.is_development else None,
                },
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "detail": exc.errors(),
                },
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            exc_type=type(exc).__name__,
            exc_str=str(exc),
            path=request.url.path,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected error occurred.",
                },
            },
        )

    # ── Routes ─────────────────────────────────────────────────────────────
    app.include_router(api_router)
    if settings.USHBOOKNPAY_BASE_PATH:
        app.include_router(api_router, prefix=settings.USHBOOKNPAY_BASE_PATH)

    return app


app = create_application()
