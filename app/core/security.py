"""
app/core/security.py
────────────────────
User authentication via interservice call to ushauth and USHSPA_TOKEN verification.

Design decisions:
- User token validation is performed by calling the ushauth service (`/api/v1/customers/me/`).
- Validated user profiles are cached in Redis for 60 seconds to minimize latency and interservice traffic.
- USHSPA_TOKEN is compared using hmac.compare_digest to prevent timing attacks.
- No secret material or credentials are ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.common.redis_client import get_redis
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.logging import get_logger

logger = get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


class TokenPayload(BaseModel):
    """Verified customer identity."""

    sub: str  # customer_id (UUID as string)
    user_type: str | None = "customer"
    phone_number: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    dob: str | None = None
    gender: str | None = None


async def validate_user_with_ushauth(
    token: str,
    settings: Settings,
) -> TokenPayload:
    """
    Validate user token by calling ushauth service (/api/v1/customers/me/).
    Caches verified user profile in Redis for 60 seconds.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cache_key = f"auth_user:{token_hash}"

    # 1. Check Redis cache
    try:
        redis = get_redis()
        cached = await redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            return TokenPayload(**data)
    except Exception as exc:
        logger.debug("auth_cache_lookup_failed", error=str(exc))

    # 2. Interservice call to ushauth
    url = f"{settings.ushauth_base_url.rstrip('/')}/api/v1/customers/me/"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-USHSPA-TOKEN": settings.USHSPA_TOKEN,
        "USHSPA-TOKEN": settings.USHSPA_TOKEN,
        "Accept": "application/json",
        # Tell ushauth's Django SecurityMiddleware that this inter-service request
        # arrived over a secure channel (TLS is terminated at the load balancer).
        # Without this header, ushauth's SECURE_SSL_REDIRECT=True returns a 301
        # redirect to https:// for every plain HTTP inter-container call.
        "X-Forwarded-Proto": "https",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.GATEWAY_TIMEOUT) as client:
            response = await client.get(url, headers=headers)

        if response.status_code in (401, 403):
            logger.warning("ushauth_token_rejected", status_code=response.status_code)
            raise AuthenticationError("Invalid or expired token.")

        response.raise_for_status()
        user_data = response.json()

    except AuthenticationError:
        raise
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ushauth_auth_http_error",
            status=exc.response.status_code,
            detail=exc.response.text[:200],
        )
        raise AuthenticationError("Failed to validate user token with auth service.")
    except Exception as exc:
        logger.error("ushauth_auth_connection_error", url=url, error=str(exc))
        raise AuthenticationError(f"Auth service unreachable at {url}.")

    # 3. Construct TokenPayload (handle envelopes like {"data": {...}} or {"result": {...}})
    profile = user_data
    if isinstance(user_data, dict):
        if isinstance(user_data.get("data"), dict):
            profile = user_data["data"]
        elif isinstance(user_data.get("result"), dict):
            profile = user_data["result"]
        elif isinstance(user_data.get("user"), dict):
            profile = user_data["user"]
        elif isinstance(user_data.get("customer"), dict):
            profile = user_data["customer"]

    sub = ""
    if isinstance(profile, dict):
        sub = str(
            profile.get("id")
            or profile.get("customer_id")
            or profile.get("user_id")
            or profile.get("sub")
            or profile.get("uuid")
            or profile.get("pk")
            or ""
        )

    # Fallback: Extract user_id/sub from token payload directly if auth service verified token
    if not sub:
        try:
            import base64
            parts = token.split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                jwt_claims = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
                sub = str(
                    jwt_claims.get("user_id")
                    or jwt_claims.get("sub")
                    or jwt_claims.get("id")
                    or ""
                )
        except Exception:
            pass

    if not sub:
        logger.error("invalid_user_profile_payload", raw_data=user_data)
        raise AuthenticationError("Invalid user profile returned from auth service.")

    payload = TokenPayload(
        sub=sub,
        user_type=profile.get("user_type", "customer") if isinstance(profile, dict) else "customer",
        phone_number=profile.get("phone_number") if isinstance(profile, dict) else None,
        email=profile.get("email") if isinstance(profile, dict) else None,
        first_name=profile.get("first_name") if isinstance(profile, dict) else None,
        last_name=profile.get("last_name") if isinstance(profile, dict) else None,
        dob=str(profile.get("dob")) if isinstance(profile, dict) and profile.get("dob") else None,
        gender=profile.get("gender") if isinstance(profile, dict) else None,
    )

    # 4. Cache verified user in Redis (60 seconds)
    try:
        await redis.setex(cache_key, 60, payload.model_dump_json())
    except Exception as exc:
        logger.debug("auth_cache_save_failed", error=str(exc))

    return payload


def verify_ushspa_token(provided: str, settings: Settings) -> None:
    """
    Constant-time comparison of the application token.
    Checks against USHSPA_TOKEN and USH_TOKEN.

    Raises:
        AuthorizationError: if the token does not match.
    """
    tokens = [
        t for t in [
            getattr(settings, "USHSPA_TOKEN", None),
            getattr(settings, "USH_TOKEN", None),
        ] if t
    ]
    for expected in tokens:
        if hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            return
    raise AuthorizationError("Invalid USH_TOKEN / USHSPA_TOKEN.")


# ── FastAPI Dependencies ──────────────────────────────────────────────────────


def _get_ushspa_token(request: Request) -> str:
    """Extract application token from request headers or query params (USH_TOKEN, X-USHSPA-TOKEN, etc.)."""
    token = (
        request.headers.get("USH_TOKEN")
        or request.headers.get("ush_token")
        or request.headers.get("USH-TOKEN")
        or request.headers.get("ush-token")
        or request.headers.get("X-USH-TOKEN")
        or request.headers.get("x-ush-token")
        or request.headers.get("X-USHSPA-TOKEN")
        or request.headers.get("x-ushspa-token")
        or request.headers.get("USHSPA-TOKEN")
        or request.headers.get("ushspa-token")
        or request.headers.get("USHSPA_TOKEN")
        or request.headers.get("ushspa_token")
        or request.query_params.get("ush_token")
        or request.query_params.get("USH_TOKEN")
        or request.query_params.get("token")
        or request.query_params.get("ushspa_token")
        or request.query_params.get("USHSPA_TOKEN")
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="USH_TOKEN / X-USHSPA-TOKEN header is required.",
        )
    return token


async def require_app_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """
    FastAPI dependency: validates USHSPA_TOKEN on every request.

    Use for endpoints accessible without JWT (e.g. webhooks).
    """
    token = _get_ushspa_token(request)
    try:
        verify_ushspa_token(token, settings)
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
        )


async def require_authenticated_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> TokenPayload:
    """
    FastAPI dependency: validates USHSPA_TOKEN and validates user token
    via interservice call to ushauth service (/api/v1/customers/me/).

    Returns the validated TokenPayload containing customer identity.
    """
    # 1. Validate application token
    app_token = _get_ushspa_token(request)
    try:
        verify_ushspa_token(app_token, settings)
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
        )

    # 2. Extract Bearer token
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. Validate user token with ushauth service
    try:
        payload = await validate_user_with_ushauth(credentials.credentials, settings)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


async def require_internal_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """
    FastAPI dependency for internal endpoints.

    Only requires USHSPA_TOKEN (called by other microservices).
    """
    await require_app_token(request, settings)
