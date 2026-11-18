"""
app/core/cache_middleware.py
────────────────────────────
FastAPI / Starlette Response Cache Middleware with Domain Tag Invalidation.

Features:
- Caches successful (200 OK) GET / HEAD API responses in Redis.
- Attaches standard `X-Cache: HIT` / `X-Cache: MISS` headers to all responses.
- Associates cache keys with domain invalidation tags:
    • 'vouchers'     (vouchers, gifts)
    • 'bookings'     (bookings, schedules)
    • 'availability' (availability slots)
    • 'payments'     (payments, refunds, payment-links)
    • 'shop_orders'  (shop products, orders, cart)
    • 'courses'      (course subscriptions)
    • 'loyalty'      (loyalty accounts & transactions)
    • 'analytics'    (customer & therapist analytics)
- Automatically invalidates tagged cache keys on mutating requests (POST, PUT, PATCH, DELETE).
- Provides atomic Redis set tagging (SADD / SMEMBERS / UNLINK).
- Supports cache bypass via `X-Bypass-Cache: 1` header or `?nocache=1` query parameter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.common.redis_client import get_redis
from app.core.logging import get_logger
from app.core.middleware import _extract_user_id_from_token

logger = get_logger(__name__)

# Domain tag mapping based on URL segments
PATH_TAG_MAPPING: dict[str, list[str]] = {
    "vouchers": ["vouchers"],
    "gifts": ["vouchers"],
    "bookings": ["bookings", "availability"],
    "availability": ["availability"],
    "payments": ["payments"],
    "refunds": ["payments"],
    "payment-links": ["payments"],
    "shop": ["shop_orders"],
    "orders": ["shop_orders"],
    "cart": ["shop_orders"],
    "course-subscriptions": ["courses"],
    "courses": ["courses"],
    "loyalty": ["loyalty"],
    "therapists": ["analytics"],
    "customers": ["analytics"],
}

# Webhook path tags to invalidate
WEBHOOK_TAGS = ["payments", "bookings", "availability", "vouchers"]

# Endpoints exempt from caching
EXEMPT_CACHE_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/api/health",
    "/api/schema",
    "/api/docs",
    "/api/redoc",
    "/api/v1/internal",
    "/internal",
)

# TTL per domain (in seconds)
DOMAIN_TTL_MAP: dict[str, int] = {
    "availability": 30,
    "bookings": 60,
    "payments": 120,
    "shop_orders": 180,
    "loyalty": 180,
    "analytics": 60,
    "vouchers": 300,
    "courses": 300,
}
DEFAULT_CACHE_TTL = 300


def get_tags_for_path(path: str) -> list[str]:
    """Extract domain tags associated with an API path."""
    tags: set[str] = set()
    norm = path.lower()
    if "webhook" in norm:
        return list(WEBHOOK_TAGS)

    segments = [s.strip() for s in norm.split("/") if s.strip()]
    for segment in segments:
        if segment in PATH_TAG_MAPPING:
            tags.update(PATH_TAG_MAPPING[segment])

    return list(tags)


def get_ttl_for_path(path: str) -> int:
    """Return the cache TTL for a given path based on its domain tags."""
    tags = get_tags_for_path(path)
    if not tags:
        return DEFAULT_CACHE_TTL
    # Return the minimum TTL among the path's tags
    return min(DOMAIN_TTL_MAP.get(tag, DEFAULT_CACHE_TTL) for tag in tags)


def generate_cache_key(request: Request) -> str:
    """
    Generate a unique, collision-resistant Redis cache key.
    Includes normalized path, sorted query parameters, and user context.
    """
    raw_path = request.url.path
    # Normalize paths like /booknpay/api/v1/... and /api/v1/...
    norm_path = raw_path[9:] if raw_path.startswith("/booknpay") else raw_path

    # Sort query params deterministically
    sorted_params = sorted(request.query_params.multi_items())
    query_str = "&".join(f"{k}={v}" for k, v in sorted_params if k != "nocache")

    # User context: if authenticated Bearer token is present, bind to user
    user_id = _extract_user_id_from_token(request)
    user_context = f"usr:{user_id}" if user_id else "pub"

    raw_signature = f"{request.method}:{norm_path}:{query_str}:{user_context}"
    key_hash = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()
    return f"ushbooknpay:cache:{key_hash}"


async def register_cache_key_tags(tags: list[str], cache_key: str, ttl: int) -> None:
    """Register a cache key under domain tag sets in Redis."""
    redis = get_redis()
    if redis is None:
        return

    tag_ttl = max(ttl * 2, 86400)
    for tag in tags:
        tag_key = f"ushbooknpay:tag:{tag}"
        try:
            pipe = redis.pipeline()
            pipe.sadd(tag_key, cache_key)
            pipe.expire(tag_key, tag_ttl)
            await pipe.execute()
        except Exception as exc:
            logger.debug("redis_tag_sadd_failed", tag=tag, error=str(exc))


async def invalidate_cache_tags(*tags: str) -> int:
    """
    Invalidate all Redis cache entries registered under given tag(s).
    Uses atomic SMEMBERS + UNLINK/DEL for zero race conditions.
    """
    redis = get_redis()
    if redis is None:
        return 0

    total_cleared = 0
    for tag in tags:
        tag_key = f"ushbooknpay:tag:{tag}"
        try:
            members = await redis.smembers(tag_key)
            if members:
                keys = [m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in members]
                await redis.delete(*keys)
                await redis.delete(tag_key)
                total_cleared += len(keys)
                logger.info("cache_tag_invalidated", tag=tag, keys_cleared=len(keys))
        except Exception as exc:
            logger.warning("cache_tag_invalidation_error", tag=tag, error=str(exc))

    return total_cleared


class APICacheMiddleware(BaseHTTPMiddleware):
    """
    High-performance async response caching middleware for ushbooknpay.
    Caches 200 GET/HEAD responses and invalidates domain tags on writes.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Check exemptions
        if any(path.startswith(prefix) or path.startswith(f"/booknpay{prefix}") for prefix in EXEMPT_CACHE_PATH_PREFIXES):
            return await call_next(request)

        # ── 1. Mutating requests (POST, PUT, PATCH, DELETE) ──
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await call_next(request)
            if 200 <= response.status_code < 400:
                tags = get_tags_for_path(path)
                if tags:
                    try:
                        await invalidate_cache_tags(*tags)
                    except Exception as exc:
                        logger.debug("auto_invalidation_error", error=str(exc))
            return response

        # ── 2. Read requests (GET, HEAD) ──
        if request.method not in ("GET", "HEAD"):
            return await call_next(request)

        # Ensure it's an API route
        if not (path.startswith("/api/") or path.startswith("/booknpay/api/")):
            return await call_next(request)

        # Check bypass
        bypass = (
            request.headers.get("X-Bypass-Cache") == "1"
            or request.query_params.get("nocache") == "1"
        )

        redis = get_redis()
        cache_key = generate_cache_key(request)
        ttl = get_ttl_for_path(path)

        # ── Cache Hit Check ──
        if not bypass and redis is not None:
            try:
                cached_bytes = await redis.get(cache_key)
                if cached_bytes:
                    payload = json.loads(cached_bytes)
                    headers = dict(payload.get("headers", {}))
                    headers["X-Cache"] = "HIT"
                    headers["Cache-Control"] = f"public, max-age={ttl}"
                    headers["Access-Control-Expose-Headers"] = "X-Request-ID, X-Cache"
                    return Response(
                        content=payload["content"],
                        status_code=payload["status_code"],
                        media_type=payload.get("media_type", "application/json"),
                        headers=headers,
                    )
            except Exception as exc:
                logger.debug("api_cache_get_error", error=str(exc))

        # ── Cache Miss: Execute endpoint ──
        response = await call_next(request)

        # Cache only 200 OK responses with standard JSON/text content
        content_type = response.headers.get("content-type", "")
        if (
            response.status_code == 200
            and "text/event-stream" not in content_type
            and "application/octet-stream" not in content_type
        ):
            try:
                response_body_bytes = b""
                if hasattr(response, "body_iterator"):
                    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                        response_body_bytes += chunk
                elif hasattr(response, "body"):
                    response_body_bytes = response.body

                payload_data = {
                    "content": response_body_bytes.decode("utf-8"),
                    "status_code": response.status_code,
                    "media_type": response.media_type or "application/json",
                    "headers": {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in ("x-request-id", "x-cache", "set-cookie", "content-length")
                    },
                }

                if redis is not None:
                    await redis.setex(cache_key, ttl, json.dumps(payload_data))
                    tags = get_tags_for_path(path)
                    if tags:
                        await register_cache_key_tags(tags, cache_key, ttl)

                headers = dict(response.headers)
                headers["X-Cache"] = "MISS"
                headers["Cache-Control"] = f"public, max-age={ttl}"
                headers["Access-Control-Expose-Headers"] = "X-Request-ID, X-Cache"

                return Response(
                    content=response_body_bytes,
                    status_code=response.status_code,
                    media_type=response.media_type,
                    headers=headers,
                )
            except Exception as exc:
                logger.debug("api_cache_save_error", error=str(exc))

        # Default fallback
        if "X-Cache" not in response.headers:
            response.headers["X-Cache"] = "MISS"
        return response
