"""
tests/unit/test_ushauth_client_and_middleware.py
─────────────────────────────────────────────────
Unit tests for USHAuthClient error handling and IdempotencyMiddleware robustness.
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.exceptions import (
    AuthorizationError,
    GatewayError,
    GatewayTimeoutError,
    NotFoundError,
)
from app.integrations.ushauth_client import USHAuthClient
from app.core.middleware import IdempotencyMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.USHSPA_TOKEN = "test-token"
    settings.GATEWAY_TIMEOUT = 5
    settings.ushauth_base_url = "http://testserver"
    return settings


@pytest.mark.asyncio
async def test_ushauth_client_get_404_raises_not_found(mock_settings):
    mock_http = AsyncMock()
    mock_req = httpx.Request("GET", "http://testserver/api/v1/resource/")
    mock_resp = httpx.Response(404, request=mock_req, text="Not found")
    mock_http.get.return_value = mock_resp

    client = USHAuthClient(http_client=mock_http, redis_client=MagicMock(), settings=mock_settings)

    with pytest.raises(NotFoundError) as exc_info:
        await client._get("/api/v1/resource/")
    assert "Resource not found in ushauth" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ushauth_client_get_401_raises_authorization_error(mock_settings):
    mock_http = AsyncMock()
    mock_req = httpx.Request("GET", "http://testserver/api/v1/resource/")
    mock_resp = httpx.Response(401, request=mock_req, text="Unauthorized")
    mock_http.get.return_value = mock_resp

    client = USHAuthClient(http_client=mock_http, redis_client=MagicMock(), settings=mock_settings)

    with pytest.raises(AuthorizationError) as exc_info:
        await client._get("/api/v1/resource/")
    assert "authentication/authorization failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ushauth_client_get_500_raises_gateway_error(mock_settings):
    mock_http = AsyncMock()
    mock_req = httpx.Request("GET", "http://testserver/api/v1/resource/")
    mock_resp = httpx.Response(500, request=mock_req, text="Internal Server Error")
    mock_http.get.return_value = mock_resp

    client = USHAuthClient(http_client=mock_http, redis_client=MagicMock(), settings=mock_settings)

    with pytest.raises(GatewayError) as exc_info:
        await client._get("/api/v1/resource/")
    assert "ushauth returned 500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ushauth_client_get_timeout_raises_gateway_timeout(mock_settings):
    mock_http = AsyncMock()
    mock_http.get.side_effect = httpx.TimeoutException("Connection timed out")

    client = USHAuthClient(http_client=mock_http, redis_client=MagicMock(), settings=mock_settings)

    with pytest.raises(GatewayTimeoutError) as exc_info:
        await client._get("/api/v1/resource/")
    assert "request timed out" in str(exc_info.value)


@pytest.mark.asyncio
async def test_idempotency_middleware_fallback_on_corrupt_json():
    """Ensure that if downstream returns non-JSON bytes, IdempotencyMiddleware does not leave body empty."""
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    middleware = IdempotencyMiddleware(app=MagicMock(), redis_client=mock_redis)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/bookings/",
        "headers": [(b"idempotency-key", b"test-key-123")],
    }
    async def receive():
        return {"type": "http.request", "body": b'{"some": "payload"}'}

    request = Request(scope, receive)

    # downstream returns raw non-JSON bytes
    async def call_next(req):
        return Response(content=b"not-json", status_code=200, media_type="text/plain")

    response = await middleware.dispatch(request, call_next)
    assert response.status_code == 200
    assert response.body == b"not-json"
