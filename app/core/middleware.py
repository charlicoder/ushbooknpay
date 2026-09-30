"""
app/core/middleware.py
──────────────────────
Production-grade middleware stack:

1. RequestIDMiddleware     — injects X-Request-ID into every request/response
2. SecureHeadersMiddleware — HSTS, X-Content-Type-Options, etc.
3. RateLimitMiddleware     — sliding-window Redis rate limiting per user/IP
                             • Authenticated requests: keyed per user_id (JWT sub)
                             • Anonymous requests: keyed per real client IP
                             • Extracts real IP from X-Forwarded-For / X-Real-IP
                             • Bypasses limiting for trusted private/internal subnets
4. IdempotencyMiddleware   — deduplicates write requests via Idempotency-Key header
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Private/internal RFC-1918 and Docker networks — bypass rate limiting for
# service-to-service calls that share a Docker bridge IP.
_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),   # covers 172.16–172.31 (Docker default)
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _is_private_ip(ip: str) -> bool:
    """Return True if *ip* belongs to a private / loopback network."""
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def _get_real_client_ip(request: Request) -> str:
    """
    Resolve the real originating client IP.

    Priority:
      1. X-Forwarded-For (leftmost non-private address — set by nginx/proxy)
      2. X-Real-IP (set by some reverse proxies)
      3. request.client.host (direct connection — may be Docker gateway IP)
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # XFF may be "client, proxy1, proxy2" — take the leftmost public IP
        for candidate in (ip.strip() for ip in xff.split(",")):
            if candidate and not _is_private_ip(candidate):
                return candidate
        # All hops are private — use the leftmost anyway (internal-only deploy)
        first = xff.split(",")[0].strip()
        if first:
            return first

    x_real = request.headers.get("X-Real-IP", "").strip()
    if x_real:
        return x_real

    return request.client.host if request.client else "unknown"


def _extract_user_id_from_token(request: Request) -> str | None:
    """
    Extract user_id from the Bearer token *without* full validation.

    We only need the subject claim for the rate-limit key; full JWT validation
    happens inside the route handler. A malformed/missing token returns None.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        token = auth.split(" ", 1)[1]
        # JWT payload is the middle segment, base64url-encoded
        payload_b64 = token.split(".")[1]
        # Pad to a multiple-of-4 for standard base64 decoding
        padding = 4 - len(payload_b64) % 4
        payload_b64 += "=" * (padding % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        uid = payload.get("user_id") or payload.get("sub") or ""
        return str(uid) if uid else None
    except Exception:
        return None

# ── 1. Request ID ─────────────────────────────────────────────────────────────


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Injects a unique X-Request-ID into every request/response.

    If the client sends X-Request-ID, it is preserved; otherwise a UUID4 is generated.
    The request_id is also bound to the structlog context for the duration of the request.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── 2. Secure Headers ─────────────────────────────────────────────────────────


class SecureHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security response headers to every response.

    Based on OWASP Secure Headers Project recommendations.
    """

    _HEADERS: dict[str, str] = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Cache-Control": "no-store",
    }

    def __init__(self, app: ASGIApp, *, is_production: bool = False) -> None:
        super().__init__(app)
        if is_production:
            self._HEADERS = {
                **self._HEADERS,
                "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
            }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        for header, value in self._HEADERS.items():
            response.headers[header] = value
        return response


# ── 3. Rate Limiting ──────────────────────────────────────────────────────────


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiting backed by Redis.

    Rate-limit key strategy (in priority order):
      1. ``rate:user:<user_id>``  — for authenticated requests (Bearer token present).
         Each user gets their own independent bucket regardless of which IP they
         connect from or whether they share a Docker bridge gateway with other users.
      2. ``rate:ip:<real_client_ip>``  — for unauthenticated requests.
         Real IP is extracted from X-Forwarded-For / X-Real-IP headers so that
         traffic routed through an nginx proxy is not all bucketed under the
         proxy's IP address.

    Private-network bypass:
      Requests arriving from RFC-1918 / loopback addresses (10.x, 172.16-31.x,
      192.168.x, 127.x) with *no* public X-Forwarded-For are exempted from rate
      limiting entirely. This covers inter-service calls inside a Docker network
      where all containers share the same bridge gateway IP.

    Skips: /health, /ready endpoints.
    """

    _SKIP_PATHS: frozenset[str] = frozenset({"/api/v1/health/", "/api/v1/ready/"})

    def __init__(
        self,
        app: ASGIApp,
        *,
        redis_client: Any,
        limit: int = 300,
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        self._redis = redis_client
        self._limit = limit
        self._window = window_seconds

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        real_ip = _get_real_client_ip(request)

        # Bypass rate limiting for internal/Docker service-to-service calls.
        if _is_private_ip(real_ip):
            return await call_next(request)

        # Prefer per-user keying for authenticated requests.
        user_id = _extract_user_id_from_token(request)
        if user_id:
            key = f"rate:user:{user_id}"
            key_label = f"user:{user_id}"
        else:
            key = f"rate:ip:{real_ip}"
            key_label = f"ip:{real_ip}"

        try:
            pipe = self._redis.pipeline()
            await pipe.incr(key)
            await pipe.expire(key, self._window)
            results = await pipe.execute()
            count = results[0]
        except Exception as exc:
            logger.warning("rate_limit_redis_error", error=str(exc))
            # Fail open — do not block requests when Redis is unavailable
            return await call_next(request)

        if count > self._limit:
            logger.warning(
                "rate_limit_exceeded",
                client_ip=real_ip,
                rate_key=key_label,
                count=count,
                limit=self._limit,
                method=request.method,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests. Please try again later.",
                    },
                },
                headers={
                    "Retry-After": str(self._window),
                    "X-RateLimit-Limit": str(self._limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self._limit - count))
        return response


# ── 4. Idempotency ────────────────────────────────────────────────────────────


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """
    Request deduplication for write operations via Idempotency-Key header.

    Only applies to POST requests.
    Stores a hash of the response body in Redis for TTL seconds.
    On a duplicate request (same key, same body), returns the cached response.
    On a conflicting request (same key, different body), returns 409.

    Key format: idempotency:<idempotency_key>
    """

    _APPLY_METHODS: frozenset[str] = frozenset({"POST"})
    _SKIP_PATHS: frozenset[str] = frozenset(
        {"/api/v1/health/", "/api/v1/ready/", "/api/v1/payments/webhook/"}
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        redis_client: Any,
        ttl_seconds: int = 86400,
    ) -> None:
        super().__init__(app)
        self._redis = redis_client
        self._ttl = ttl_seconds

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method not in self._APPLY_METHODS:
            return await call_next(request)

        if any(request.url.path.startswith(skip) for skip in self._SKIP_PATHS):
            return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        redis_key = f"idempotency:{idempotency_key}"

        # Read request body (need to buffer it for hashing)
        body = await request.body()
        body_hash = hashlib.sha256(body).hexdigest()

        try:
            stored = await self._redis.get(redis_key)
        except Exception as exc:
            logger.warning("idempotency_redis_error", error=str(exc))
            return await call_next(request)

        if stored is not None:
            stored_data = json.loads(stored)
            if stored_data["body_hash"] != body_hash:
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": {
                            "code": "IDEMPOTENCY_CONFLICT",
                            "message": (
                                "Idempotency-Key already used with a different request body."
                            ),
                        },
                    },
                )
            # Return the cached response
            return JSONResponse(
                status_code=stored_data["status_code"],
                content=stored_data["response_body"],
                headers={"X-Idempotent-Replayed": "true"},
            )

        # Proceed with the request
        response = await call_next(request)

        # Cache successful responses (2xx)
        if 200 <= response.status_code < 300:
            try:
                response_body_bytes = b""
                if hasattr(response, "body_iterator"):
                    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                        response_body_bytes += chunk
                elif hasattr(response, "body"):
                    response_body_bytes = response.body

                response_body = json.loads(response_body_bytes.decode())
                await self._redis.setex(
                    redis_key,
                    self._ttl,
                    json.dumps(
                        {
                            "body_hash": body_hash,
                            "status_code": response.status_code,
                            "response_body": response_body,
                        }
                    ),
                )
                return JSONResponse(
                    status_code=response.status_code,
                    content=response_body,
                    headers=dict(response.headers),
                )
            except Exception as exc:
                logger.warning("idempotency_cache_store_error", error=str(exc))
                return Response(
                    content=response_body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )

        return response
