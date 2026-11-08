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
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.v1.router import api_router
from app.booking.infrastructure.models import Booking, BookingStatusHistory, TemporaryHold  # noqa: F401
from app.booking.infrastructure.cache_outbox import AppointmentCacheOutbox  # noqa: F401
from app.loyalty.infrastructure.models import LoyaltyAccount, LoyaltyTransaction  # noqa: F401
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
from app.payment.infrastructure.models import Payment, PaymentStatusHistory, Refund  # noqa: F401
from app.payment_links.infrastructure.models import PaymentLink, PaymentWebhookEvent  # noqa: F401
from app.voucher.infrastructure.models import GiftVoucher  # noqa: F401
from app.course.infrastructure.models import CourseSessionUsage, CourseSubscription  # noqa: F401
from app.events.outbox.models import OutboxEvent  # noqa: F401
from app.gifts.infrastructure.models import (  # noqa: F401
    GiftVoucherCart,
    GiftVoucherCartItem,
    GiftVoucherPurchase,
    GiftVoucherPurchaseItem,
    GiftVoucherRecipient,
    GiftVoucherDelivery,
    GiftVoucherVerification,
    GiftVoucherRedemption,
    GiftVoucherStatusHistory,
)

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
        import sqlalchemy as sa
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            try:
                await conn.execute(
                    sa.text(
                        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_refunded NUMERIC(10, 3) NOT NULL DEFAULT 0.000;"
                    )
                )
            except Exception as col_exc:
                logger.debug("ensure_amount_refunded_column_note", error=str(col_exc))
            for _ddl in (
                "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(150);",
                "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS credit_note_id VARCHAR(64);",
                "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;",
                "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS failure_reason TEXT;",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_refunds_idempotency_key ON refunds (idempotency_key);",
                "ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS services JSONB;",
                "ALTER TABLE gift_vouchers ADD COLUMN IF NOT EXISTS redemptions JSONB;",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS branch_name VARCHAR(255);",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS branch_name VARCHAR(255);",
                # Course subscriptions (migration 0064) — keep dev DBs in sync
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS course_subscription_id UUID;",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS course_data JSONB;",
                "CREATE INDEX IF NOT EXISTS ix_bookings_course_subscription_id ON bookings (course_subscription_id);",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS course_subscription_id UUID;",
                "CREATE INDEX IF NOT EXISTS ix_payments_course_subscription_id ON payments (course_subscription_id);",
                # Drop legacy doubled check constraints produced by earlier Alembic helpers
                "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_booking_type;",
                "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_ck_bookings_payment_type;",
                # Recreate the type checks only when they don't know the course values yet
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
                "'ck_bookings_booking_type' AND pg_get_constraintdef(oid) LIKE '%course_redemption%') THEN "
                "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_type; "
                "ALTER TABLE bookings ADD CONSTRAINT ck_bookings_booking_type CHECK (booking_type IN "
                "('branch_service', 'home_service', 'loyalty', 'gift_voucher', 'course_redemption')); "
                "END IF; END $$;",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
                "'ck_bookings_payment_type' AND pg_get_constraintdef(oid) LIKE '%course_subscription%') THEN "
                "ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_payment_type; "
                "ALTER TABLE bookings ADD CONSTRAINT ck_bookings_payment_type CHECK (payment_type IN "
                "('service', 'gift_voucher', 'rewarded', 'course_subscription')); "
                "END IF; END $$;",
            ):
                try:
                    await conn.execute(sa.text(_ddl))
                except Exception as col_exc:
                    logger.debug("ensure_refund_lifecycle_column_note", error=str(col_exc))
        logger.info("database_schema_initialized")
    except Exception as exc:
        logger.warning("database_schema_init_warning", error=str(exc))

    get_session_factory()

    # Initialise Redis (validates connection)
    get_redis()

    # Initialise SQS client
    get_sqs_client()

    # Replay failed ushauth appointment-cache writes until acknowledged.
    relay_task: asyncio.Task | None = None
    if settings.CACHE_OUTBOX_ENABLED:
        from app.api.deps import get_http_client
        from app.booking.application.cache_outbox_relay import run_relay_forever
        from app.integrations.ushauth_client import USHAuthClient

        def _relay_client() -> USHAuthClient:
            return USHAuthClient(http_client=get_http_client(), redis_client=get_redis(), settings=settings)

        relay_task = asyncio.create_task(run_relay_forever(_relay_client))

    # Transactional outbox relay (course events) + course background tasks.
    background_tasks: list[asyncio.Task] = []
    if settings.OUTBOX_RELAY_ENABLED:
        from app.events.outbox.relay import run_outbox_relay_forever

        background_tasks.append(asyncio.create_task(run_outbox_relay_forever()))
    if settings.COURSE_TASKS_ENABLED:
        from app.course.application.tasks import run_expiry_forever, run_invoice_retry_forever

        background_tasks.append(asyncio.create_task(run_expiry_forever()))
        background_tasks.append(asyncio.create_task(run_invoice_retry_forever()))

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("service_shutting_down")

    if relay_task is not None:
        relay_task.cancel()
        try:
            await relay_task
        except (asyncio.CancelledError, Exception):
            pass

    for task in background_tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    await close_redis()
    await dispose_engine()

    # Close shared httpx client
    from app.api.deps import close_http_client
    await close_http_client()

    logger.info("service_stopped")


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    # Canonical payment_through / payment_provider / payment_method on every write
    from app.common.payment_attribution_listeners import register_payment_attribution_listeners

    register_payment_attribution_listeners()

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

    def _sanitize_for_json(obj: Any) -> Any:
        """Recursively convert non-serializable objects (such as Exception instances in ctx['error']) to serializable strings."""
        if isinstance(obj, Exception):
            return str(obj)
        if isinstance(obj, dict):
            return {k: _sanitize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [_sanitize_for_json(v) for v in obj]
        return obj

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
                    "detail": jsonable_encoder(_sanitize_for_json(exc.errors())),
                },
            },
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_exception_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Validation failed.",
                    "detail": jsonable_encoder(_sanitize_for_json(exc.errors())),
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
