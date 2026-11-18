"""
tests/unit/test_cache_improvements.py
─────────────────────────────────────
Unit tests for safe Redis caching improvements in ushbooknpay:
1. Customer Analytics caching (hit, miss, refresh bypass)
2. Therapist Analytics caching (hit, miss, refresh bypass)
3. Payment status polling caching for terminal states
4. Availability slot block caching
5. Voucher stats and creators caching
"""

import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.v1.customer_analytics import get_customer_analytics
from app.api.v1.therapist_analytics import get_therapist_analytics
from app.api.v1.payments import get_payment_status
from app.api.v1.availability import get_availability
from app.voucher.api.router import voucher_stats, voucher_creators


@pytest.mark.asyncio
async def test_customer_analytics_cache_hit():
    customer_id = uuid.uuid4()
    mock_redis = AsyncMock()
    cached_payload = {
        "success": True,
        "customer_id": str(customer_id),
        "loyalty": {"account_id": None, "current_tier": "Bronze", "current_points": 0, "total_spent": 0.0, "total_visits": 0, "transactions": []},
        "gift_vouchers": {"purchased_count": 0, "purchased_amount": 0.0, "received_count": 0, "received_amount": 0.0, "redeemed_count": 0, "redeemed_amount": 0.0, "active_vouchers": []},
        "financials": {"lifetime_gross_spend": 0.0, "lifetime_net_spend": 0.0, "refunded_amount": 0.0, "avg_order_value": 0.0, "total_paid_transactions": 0, "currency": "KWD", "recent_payments": []},
        "sales_and_bookings": {"total_bookings": 0, "completed_bookings": 0, "cancelled_bookings": 0, "no_show_bookings": 0, "completion_rate_pct": 0.0, "cancellation_rate_pct": 0.0, "home_service_count": 0, "spa_branch_count": 0, "recent_bookings": []},
        "marketing_analytics": {"top_services": [], "top_therapists": [], "preferred_branch": None, "preferred_time_slot": None, "total_treatment_hours": 0.0, "lifecycle_status": "New Client"},
    }
    mock_redis.get.return_value = json.dumps(cached_payload)
    mock_session = AsyncMock()

    resp = await get_customer_analytics(
        customer_id=customer_id,
        session=mock_session,
        phone=None,
        refresh=False,
        redis=mock_redis,
    )

    mock_redis.get.assert_awaited_once_with(f"analytics:customer:{customer_id}:")
    # Verify DB was never touched
    mock_session.execute.assert_not_called()
    assert resp.headers["X-Cache"] == "HIT"
    body = json.loads(resp.body.decode())
    assert body["customer_id"] == str(customer_id)
    assert body["marketing_analytics"]["lifecycle_status"] == "New Client"


@pytest.mark.asyncio
async def test_therapist_analytics_cache_hit():
    therapist_id = uuid.uuid4()
    mock_redis = AsyncMock()
    cached_payload = {
        "success": True,
        "therapist_id": str(therapist_id),
        "therapist_name": "Test Therapist",
        "sales": {"total_bookings": 5, "completed_bookings": 5, "cancelled_bookings": 0, "no_show_bookings": 0, "completion_rate_pct": 100.0, "cancellation_rate_pct": 0.0, "no_show_rate_pct": 0.0, "avg_ticket_value": 25.0, "monthly_trends": []},
        "marketing": {"total_unique_clients": 3, "repeat_clients_count": 1, "repeat_client_rate_pct": 33.3, "single_visit_clients": 2, "top_services": [], "top_branches": [], "client_time_preferences": [], "home_service_count": 0, "spa_branch_count": 5, "top_clients": []},
        "financials": {"total_gross_revenue": 125.0, "completed_revenue": 125.0, "pending_revenue": 0.0, "addon_revenue": 0.0, "extra_minutes_revenue": 0.0, "refunded_amount": 0.0, "net_revenue": 125.0, "estimated_commission": 12.5, "commission_rate_pct": 10.0, "avg_revenue_per_hour": 25.0, "currency": "KWD", "total_transactions": 5},
        "performance": {"total_treatment_hours": 5.0, "total_treatment_minutes": 300, "extra_treatment_minutes": 0, "addons_treatment_minutes": 0, "average_service_duration_minutes": 60.0, "completion_rate_pct": 100.0, "reliability_score_pct": 100.0, "busiest_day": "Monday", "busiest_time_slot": "10:00", "day_distribution": [], "first_appointment_date": None, "latest_appointment_date": None},
        "recent_bookings": [],
    }
    mock_redis.get.return_value = json.dumps(cached_payload)
    mock_session = AsyncMock()

    resp = await get_therapist_analytics(
        therapist_id=therapist_id,
        session=mock_session,
        refresh=False,
        redis=mock_redis,
    )

    mock_redis.get.assert_awaited_once_with(f"analytics:therapist:{therapist_id}")
    # Verify DB was never touched
    mock_session.execute.assert_not_called()
    assert resp.headers["X-Cache"] == "HIT"
    body = json.loads(resp.body.decode())
    assert body["therapist_id"] == str(therapist_id)
    assert body["therapist_name"] == "Test Therapist"


@pytest.mark.asyncio
async def test_payment_status_cache_hit():
    booking_id = uuid.uuid4()
    mock_redis = AsyncMock()
    cached_content = {
        "success": True,
        "data": {
            "payment_id": str(uuid.uuid4()),
            "payment_number": "PMT-001",
            "invoice_number": "INV-001",
            "booking_id": str(booking_id),
            "status": "success",
            "total_amount": "25.000",
            "currency": "KWD",
        },
    }
    mock_redis.get.return_value = json.dumps(cached_content)
    mock_session = AsyncMock()
    mock_user = MagicMock()

    resp = await get_payment_status(
        booking_id=booking_id,
        current_user=mock_user,
        session=mock_session,
        redis=mock_redis,
    )

    mock_redis.get.assert_awaited_once_with(f"payment_status:{booking_id}")
    mock_session.execute.assert_not_called()
    assert resp.headers["X-Cache"] == "HIT"
    body = json.loads(resp.body.decode())
    assert body["success"] is True
    assert body["data"]["status"] == "success"


@pytest.mark.asyncio
async def test_availability_cache_hit():
    branch_id = uuid.uuid4()
    service_id = uuid.uuid4()
    mock_redis = AsyncMock()
    cached_content = {
        "success": True,
        "data": [{"therapist_id": "t-1", "availability": {}}],
        "meta": {"branch_id": str(branch_id), "service_id": str(service_id)},
    }
    mock_redis.get.return_value = json.dumps(cached_content)

    mock_session = AsyncMock()
    mock_ushauth = AsyncMock()
    mock_settings = MagicMock()

    resp = await get_availability(
        _=None,
        session=mock_session,
        ushauth=mock_ushauth,
        settings=mock_settings,
        branch_id=branch_id,
        service_id=service_id,
        redis=mock_redis,
    )

    assert mock_redis.get.await_count == 1
    # Catalog calls not needed on cache hit
    mock_ushauth.get_branch.assert_not_called()
    assert resp.headers["X-Cache"] == "HIT"
    body = json.loads(resp.body.decode())
    assert body["success"] is True
    assert len(body["data"]) == 1


@pytest.mark.asyncio
async def test_voucher_creators_cache_hit():
    mock_redis = AsyncMock()
    cached_data = {"success": True, "data": [{"id": "u-1", "name": "Admin", "role": "admin"}]}
    mock_redis.get.return_value = json.dumps(cached_data)
    mock_session = AsyncMock()

    resp = await voucher_creators(
        _=None,
        session=mock_session,
        redis=mock_redis,
    )

    mock_redis.get.assert_awaited_once_with("voucher:creators")
    mock_session.execute.assert_not_called()
    assert resp.headers["X-Cache"] == "HIT"
    body = json.loads(resp.body.decode())
    assert body["data"][0]["name"] == "Admin"


@pytest.mark.asyncio
async def test_voucher_stats_cache_hit():
    mock_redis = AsyncMock()
    cached_data = {"success": True, "data": {"total": 42, "active": 10, "revenue": 1000.0}}
    mock_redis.get.return_value = json.dumps(cached_data)
    mock_session = AsyncMock()

    resp = await voucher_stats(
        _=None,
        session=mock_session,
        redis=mock_redis,
    )

    assert mock_redis.get.await_count == 1
    mock_session.execute.assert_not_called()
    assert resp.headers["X-Cache"] == "HIT"
    body = json.loads(resp.body.decode())
    assert body["data"]["total"] == 42


@pytest.mark.asyncio
async def test_payment_status_terminal_caching():
    booking_id = uuid.uuid4()
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None  # Cache miss

    from datetime import datetime, timezone

    payment = MagicMock()
    payment.id = uuid.uuid4()
    payment.payment_number = "PMT1"
    payment.invoice_number = "INV1"
    payment.booking_id = booking_id
    payment.booking_data = None
    payment.voucher_id = None
    payment.voucher_number = None
    payment.payment_for = "booking"
    payment.payment_provider = "myfatoorah"
    payment.status = "success"  # Terminal state
    payment.total_amount = "44.000"
    payment.currency = "KWD"
    payment.payment_method = "knet"
    payment.reference_id = "REF123"
    payment.created_at = datetime.now(timezone.utc)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = payment
    mock_session.execute.return_value = mock_result
    mock_user = MagicMock()

    resp = await get_payment_status(
        booking_id=booking_id,
        current_user=mock_user,
        session=mock_session,
        redis=mock_redis,
    )

    mock_redis.get.assert_awaited_once_with(f"payment_status:{booking_id}")
    # Verify cached for 600 seconds
    mock_redis.setex.assert_awaited_once()
    assert mock_redis.setex.call_args[0][0] == f"payment_status:{booking_id}"
    assert mock_redis.setex.call_args[0][1] == 600
    assert resp.headers["X-Cache"] == "MISS"
    body = json.loads(resp.body.decode())
    assert body["success"] is True
    assert body["data"]["status"] == "success"


@pytest.mark.asyncio
async def test_voucher_stats_cache_miss_saves_to_redis():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None  # Cache miss
    mock_session = AsyncMock()

    with patch("app.voucher.application.desk_list.fetch_stats", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"total": 15, "active": 5}
        resp = await voucher_stats(
            _=None,
            session=mock_session,
            redis=mock_redis,
            statuses=None,
            channel=None,
            created_by=None,
            q=None,
            created_from=None,
            created_to=None,
            redeemed_from=None,
            redeemed_to=None,
            activated_from=None,
            activated_to=None,
        )

    mock_redis.setex.assert_awaited_once()
    # 30s TTL
    assert mock_redis.setex.call_args[0][1] == 30
    assert resp.headers["X-Cache"] == "MISS"
    body = json.loads(resp.body.decode())
    assert body["data"]["total"] == 15


@pytest.mark.asyncio
async def test_secure_headers_middleware_injects_x_cache_miss_and_expose_headers():
    from starlette.requests import Request
    from starlette.responses import Response
    from app.core.middleware import SecureHeadersMiddleware

    app = MagicMock()
    middleware = SecureHeadersMiddleware(app)

    async def fake_call_next(req):
        return Response("ok", media_type="text/plain")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/payments/",
        "headers": [],
    }
    request = Request(scope)
    resp = await middleware.dispatch(request, fake_call_next)

    assert resp.headers["X-Cache"] == "MISS"
    assert "X-Cache" in resp.headers["Access-Control-Expose-Headers"]
    assert "X-Request-ID" in resp.headers["Access-Control-Expose-Headers"]


@pytest.mark.asyncio
async def test_api_cache_middleware_get_and_tag_invalidation():
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from app.core.cache_middleware import APICacheMiddleware, invalidate_cache_tags

    mock_redis = AsyncMock()
    # In-memory storage for mock redis
    store = {}
    sets = {}

    async def fake_get(k):
        return store.get(k)

    async def fake_setex(k, ttl, v):
        store[k] = v

    async def fake_smembers(k):
        return sets.get(k, set())

    async def fake_delete(*keys):
        for k in keys:
            store.pop(k, None)
            sets.pop(k, None)

    class FakePipeline:
        def sadd(self, k, v):
            sets.setdefault(k, set()).add(v)

        def expire(self, k, ttl):
            pass

        async def execute(self):
            return True

    mock_redis.get.side_effect = fake_get
    mock_redis.setex.side_effect = fake_setex
    mock_redis.smembers.side_effect = fake_smembers
    mock_redis.delete.side_effect = fake_delete
    mock_redis.pipeline = MagicMock(return_value=FakePipeline())

    with patch("app.core.cache_middleware.get_redis", return_value=mock_redis):
        middleware = APICacheMiddleware(MagicMock())

        call_count = 0

        async def fake_endpoint(req):
            nonlocal call_count
            call_count += 1
            return JSONResponse({"vouchers": [1, 2, 3]}, status_code=200)

        # ── 1. First GET: cache miss ──
        req1 = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/vouchers/",
            "query_string": b"",
            "headers": [(b"x-ushspa-token", b"hello-world")],
        })
        resp1 = await middleware.dispatch(req1, fake_endpoint)
        assert resp1.headers["X-Cache"] == "MISS"
        assert resp1.headers["Cache-Control"] == "public, max-age=300"
        assert call_count == 1
        assert len(store) == 1
        assert "ushbooknpay:tag:vouchers" in sets

        # ── 2. Second GET: cache HIT ──
        req2 = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/vouchers/",
            "query_string": b"",
            "headers": [(b"x-ushspa-token", b"hello-world")],
        })
        resp2 = await middleware.dispatch(req2, fake_endpoint)
        assert resp2.headers["X-Cache"] == "HIT"
        # Endpoint was NOT called again
        assert call_count == 1

        # ── 3. Mutating write POST: auto-invalidates vouchers tag ──
        async def fake_write(req):
            return JSONResponse({"status": "created"}, status_code=201)

        req_write = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/v1/vouchers/",
            "query_string": b"",
            "headers": [],
        })
        resp_write = await middleware.dispatch(req_write, fake_write)
        assert resp_write.status_code == 201

        # ── 4. Third GET: cache MISS after invalidation ──
        req3 = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/vouchers/",
            "query_string": b"",
            "headers": [(b"x-ushspa-token", b"hello-world")],
        })
        resp3 = await middleware.dispatch(req3, fake_endpoint)
        assert resp3.headers["X-Cache"] == "MISS"
        assert call_count == 2

        # ── 5. Fourth GET with ?nocache=1: bypasses cache ──
        req4 = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v1/vouchers/",
            "query_string": b"nocache=1",
            "headers": [(b"x-ushspa-token", b"hello-world")],
        })
        resp4 = await middleware.dispatch(req4, fake_endpoint)
        assert resp4.headers["X-Cache"] == "MISS"
        assert call_count == 3


@pytest.mark.asyncio
async def test_domain_tag_mapping_and_ttls():
    from app.core.cache_middleware import get_tags_for_path, get_ttl_for_path

    assert "vouchers" in get_tags_for_path("/api/v1/vouchers/")
    assert "vouchers" in get_tags_for_path("/booknpay/api/v1/gifts/")
    assert get_ttl_for_path("/api/v1/vouchers/") == 300

    assert "bookings" in get_tags_for_path("/api/v1/bookings/")
    assert "availability" in get_tags_for_path("/api/v1/availability/")
    assert get_ttl_for_path("/api/v1/availability/") == 30

    assert "payments" in get_tags_for_path("/api/v1/payments/")
    assert "payments" in get_tags_for_path("/api/v1/refunds/")
    assert get_ttl_for_path("/api/v1/payments/") == 120

    assert "shop_orders" in get_tags_for_path("/api/v1/shop/orders/")
    assert get_ttl_for_path("/api/v1/shop/orders/") == 180

    # Webhook invalidation tags
    webhook_tags = get_tags_for_path("/api/v1/payments/webhook/myfatoorah/")
    assert "payments" in webhook_tags
    assert "bookings" in webhook_tags
