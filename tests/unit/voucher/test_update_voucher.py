"""
tests/unit/voucher/test_update_voucher.py
─────────────────────────────────────────
Unit tests for the update gift voucher API endpoint and service method:
- PATCH /api/v1/vouchers/{voucher_id}/
- PUT /api/v1/vouchers/{voucher_id}/
- GiftVoucherService.update_voucher()
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.core.exceptions import NotFoundError, ValidationError
from app.core.security import TokenPayload
from app.main import app
from app.voucher.api.router import update_gift_voucher, update_voucher_delivery_status
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import DeliveryStatus, GiftVoucherStatus
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.interfaces.schemas import (
    CreateGiftVoucherRequest,
    UpdateGiftVoucherRequest,
    UpdateVoucherDeliveryStatusRequest,
)


# ── Helpers & Mocks ───────────────────────────────────────────────────────────


def _make_mock_voucher(
    voucher_id: uuid.UUID | None = None,
    sender_id: uuid.UUID | None = None,
    status: str = "created",
    delivery_status: str | None = None,
) -> GiftVoucher:
    v = MagicMock(spec=GiftVoucher)
    v.id = voucher_id or uuid.uuid4()
    v.service_id = uuid.uuid4()
    v.gift_category = "service"
    v.service_data = {"name": "Signature Massage"}
    v.branch_id = uuid.uuid4()
    v.branch_data = {"name": "Salmiya Spa"}
    v.service_arrangement_id = None
    v.service_arrangement_data = {}
    v.addons = []
    v.extra_time = 0
    v.price_for_extra_time = None
    v.expire_date = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    v.status = status
    v.sender_id = sender_id or uuid.uuid4()
    v.sender_data = {"name": "Ahmad", "phone_number": "+96511111111"}
    v.recipient_phone = "+96522222222"
    v.recipient_id = None
    v.recipient_data = {"name": "Fatima", "phone_number": "+96522222222"}
    v.created_by = v.sender_id
    v.total_duration = 60
    v.total_amount = Decimal("40.000")
    v.currency = "KWD"
    v.gift_message = "Happy Birthday!"
    v.gift_template = "gold"
    v.secret_code = "123456"
    v.public_token = "token-abc-123"
    v.redeemed_booking_id = None
    v.redeemed_at = None
    v.redeemed_by = None
    v.booking_id = None
    v.booking_data = {}
    v.payment_id = None
    v.payment_data = None
    v.payment_url = None
    v.payment_provider = None
    v.payment_through = None
    v.ordered_items = None
    v.delivery_status = delivery_status
    v.delivery_address = None
    v.created_at = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    v.updated_at = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    v.to_snapshot = MagicMock(return_value={
        "id": str(v.id),
        "service_id": str(v.service_id),
        "service_data": v.service_data,
        "status": v.status,
        "total_amount": str(v.total_amount),
        "currency": v.currency,
        "secret_code": v.secret_code,
        "public_token": v.public_token,
    })
    return v


# ── Route Registration Tests ──────────────────────────────────────────────────


def test_update_voucher_routes_registered():
    """Verify that PATCH and PUT routes are registered for vouchers."""
    patch_routes = [
        route.path for route in app.routes
        if "PATCH" in getattr(route, "methods", set())
    ]
    put_routes = [
        route.path for route in app.routes
        if "PUT" in getattr(route, "methods", set())
    ]
    assert "/api/v1/vouchers/{voucher_id}/" in patch_routes
    assert "/api/v1/vouchers/{voucher_id}/" in put_routes
    assert "/booknpay/api/v1/vouchers/{voucher_id}/" in patch_routes
    assert "/booknpay/api/v1/vouchers/{voucher_id}/" in put_routes


# ── Service Layer Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_update_voucher_fields():
    """Verify partial updates of fields in GiftVoucherService."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)

    voucher = _make_mock_voucher()
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    updated = await svc.update_voucher(
        voucher.id,
        fields_to_update={
            "gift_message": "Enjoy your relaxing massage!",
            "total_amount": Decimal("65.000"),
            "extra_time": 30,
            "currency": "KWD",
        },
    )

    assert voucher.gift_message == "Enjoy your relaxing massage!"
    assert voucher.total_amount == Decimal("65.000")
    assert voucher.extra_time == 30
    svc._repo.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_update_voucher_not_found():
    """Verify NotFoundError is raised when voucher does not exist."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.get_by_id = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.update_voucher(uuid.uuid4(), fields_to_update={"gift_message": "test"})

    assert "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_service_update_voucher_valid_status_transition():
    """Verify valid status transition created -> active updates status and emits event."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)

    voucher = _make_mock_voucher(status="created")
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    with patch.object(svc, "_emit_status_event", new_callable=AsyncMock) as mock_emit:
        updated = await svc.update_voucher(
            voucher.id,
            fields_to_update={
                "status": "active",
                "payment_id": "MF-999888",
                "payment_data": {"invoiceId": "MF-999888"},
            },
        )
        assert voucher.status == "active"
        assert voucher.payment_id == "MF-999888"
        assert voucher.payment_data == {"invoiceId": "MF-999888"}


@pytest.mark.asyncio
async def test_service_update_voucher_redeemed_status():
    """Verify transitioning to redeemed sets redeemed_at, redeemed_by, and redeemed_booking_id."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)

    voucher = _make_mock_voucher(status="active")
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    booking_id = uuid.uuid4()
    user_id = uuid.uuid4()

    with patch.object(svc, "_emit_status_event", new_callable=AsyncMock):
        await svc.update_voucher(
            voucher.id,
            fields_to_update={
                "status": "redeemed",
                "booking_id": booking_id,
                "redeemed_by": user_id,
            },
        )
        assert voucher.status == "redeemed"
        assert voucher.redeemed_at is not None
        assert voucher.redeemed_booking_id == booking_id
        assert voucher.redeemed_by == user_id


@pytest.mark.asyncio
async def test_service_update_voucher_invalid_status_transition():
    """Verify invalid status transition raises ValidationError."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)

    voucher = _make_mock_voucher(status="created")
    svc._repo.get_by_id = AsyncMock(return_value=voucher)

    # created -> redeemed is invalid (must be active first)
    with pytest.raises(ValidationError):
        await svc.update_voucher(voucher.id, fields_to_update={"status": "redeemed"})


@pytest.mark.asyncio
async def test_service_update_voucher_delivery_status_transition():
    """Verify delivery status transition validation."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)

    voucher = _make_mock_voucher(delivery_status="ordered")
    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    # ordered -> ready_to_go is valid
    await svc.update_voucher(voucher.id, fields_to_update={"delivery_status": "ready_to_go"})
    assert voucher.delivery_status == "ready_to_go"

    # ready_to_go -> received directly is invalid
    with pytest.raises(ValidationError):
        await svc.update_voucher(voucher.id, fields_to_update={"delivery_status": "received"})


# ── Router Handler Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_update_gift_voucher_patch_success():
    """Verify PATCH /api/v1/vouchers/{voucher_id}/ handler returns updated voucher."""
    mock_session = AsyncMock()
    mock_request = MagicMock()
    mock_request.headers = Headers({"USH_TOKEN": "valid-app-token"})

    voucher = _make_mock_voucher()
    voucher.gift_message = "New special greeting"

    body = UpdateGiftVoucherRequest(gift_message="New special greeting")

    with patch("app.voucher.api.router.GiftVoucherService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        mock_svc.update_voucher = AsyncMock(return_value=voucher)

        response = await update_gift_voucher(
            voucher_id=voucher.id,
            body=body,
            _=None,
            session=mock_session,
            request=mock_request,
        )

        assert response.status_code == 200
        import json
        data = json.loads(response.body.decode())
        assert data["success"] is True
        assert data["data"]["gift_message"] == "New special greeting"
        mock_svc.update_voucher.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_update_gift_voucher_put_success():
    """Verify PUT /api/v1/vouchers/{voucher_id}/ handler returns updated voucher."""
    mock_session = AsyncMock()
    mock_request = MagicMock()
    mock_request.headers = Headers({"USH_TOKEN": "valid-app-token"})

    voucher = _make_mock_voucher()
    voucher.total_amount = Decimal("75.000")
    voucher.gift_category = "physical"

    body = UpdateGiftVoucherRequest(
        gift_category="physical",
        total_amount=Decimal("75.000"),
    )

    with patch("app.voucher.api.router.GiftVoucherService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        mock_svc.update_voucher = AsyncMock(return_value=voucher)

        response = await update_gift_voucher(
            voucher_id=voucher.id,
            body=body,
            _=None,
            session=mock_session,
            request=mock_request,
        )

        assert response.status_code == 200
        import json
        data = json.loads(response.body.decode())
        assert data["success"] is True
        assert data["data"]["gift_category"] == "physical"


@pytest.mark.asyncio
async def test_router_update_gift_voucher_not_found():
    """Verify handler raises HTTP 404 when voucher is not found."""
    mock_session = AsyncMock()
    mock_request = MagicMock()
    mock_request.headers = Headers({"USH_TOKEN": "valid-app-token"})

    body = UpdateGiftVoucherRequest(gift_message="Hello")

    with patch("app.voucher.api.router.GiftVoucherService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        mock_svc.update_voucher = AsyncMock(side_effect=NotFoundError("Gift voucher not found."))

        with pytest.raises(HTTPException) as exc_info:
            await update_gift_voucher(
                voucher_id=uuid.uuid4(),
                body=body,
                _=None,
                session=mock_session,
                request=mock_request,
            )
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_router_update_gift_voucher_invalid_transition_raises_422():
    """Verify handler raises HTTP 422 on illegal transition."""
    mock_session = AsyncMock()
    mock_request = MagicMock()
    mock_request.headers = Headers({"USH_TOKEN": "valid-app-token"})

    body = UpdateGiftVoucherRequest(status="redeemed")

    with patch("app.voucher.api.router.GiftVoucherService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        mock_svc.update_voucher = AsyncMock(side_effect=ValidationError("Cannot transition directly."))

        with pytest.raises(HTTPException) as exc_info:
            await update_gift_voucher(
                voucher_id=uuid.uuid4(),
                body=body,
                _=None,
                session=mock_session,
                request=mock_request,
            )
        assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_router_update_gift_voucher_recipient_resolution():
    """Verify that updating recipient_phone triggers ushauth resolution."""
    mock_session = AsyncMock()
    mock_request = MagicMock()
    mock_request.headers = Headers({"USH_TOKEN": "valid-app-token"})

    voucher = _make_mock_voucher()
    resolved_cust_id = uuid.uuid4()

    body = UpdateGiftVoucherRequest(
        recipient_phone="+96599887766",
        recipient_data={"name": "New Recipient"},
    )

    with patch("app.voucher.api.router.ushauth_client.get_or_create_customer") as mock_ushauth, \
         patch("app.voucher.api.router.GiftVoucherService") as mock_svc_cls:

        mock_ushauth.return_value = {
            "id": str(resolved_cust_id),
            "name": "New Recipient",
            "phone_number": "+96599887766",
            "email": "rec@example.com",
            "avatar": None,
        }
        mock_svc = mock_svc_cls.return_value
        mock_svc.update_voucher = AsyncMock(return_value=voucher)

        response = await update_gift_voucher(
            voucher_id=voucher.id,
            body=body,
            _=None,
            session=mock_session,
            request=mock_request,
        )

        assert response.status_code == 200
        mock_ushauth.assert_awaited_once()
        # Verify recipient_id was passed into update_voucher
        call_args = mock_svc.update_voucher.call_args
        assert call_args.kwargs["fields_to_update"]["recipient_id"] == resolved_cust_id


# ── Optional service_id Tests ─────────────────────────────────────────────────


def test_create_gift_voucher_request_without_service_id():
    """Verify that CreateGiftVoucherRequest allows service_id to be omitted or None."""
    req = CreateGiftVoucherRequest(
        total_amount=Decimal("50.000"),
        currency="KWD",
    )
    assert req.service_id is None

    # Empty string should coerce to None
    req2 = CreateGiftVoucherRequest(
        service_id="",
        total_amount=Decimal("50.000"),
        currency="KWD",
    )
    assert req2.service_id is None


@pytest.mark.asyncio
async def test_service_create_voucher_without_service_id():
    """Verify that GiftVoucherService.create_voucher succeeds with service_id=None."""
    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)
    svc._repo.add = MagicMock()
    svc._repo.flush = AsyncMock()

    voucher = await svc.create_voucher(
        service_id=None,
        service_data=None,
        total_amount=Decimal("30.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Test Sender"},
        gift_category="digital",
    )

    assert voucher.service_id is None
    assert voucher.gift_category == "digital"
    svc._repo.add.assert_called_once()
    svc._repo.flush.assert_awaited_once()


def test_response_schemas_allow_none_service_id():
    """Verify response models accept service_id=None without validation error."""
    voucher = _make_mock_voucher()
    voucher.service_id = None

    from app.voucher.api.router import (
        _voucher_to_list_item,
        _voucher_to_public,
        _voucher_to_response,
    )

    resp = _voucher_to_response(voucher)
    assert resp.service_id is None

    pub = _voucher_to_public(voucher)
    assert pub.service_id is None

    item = _voucher_to_list_item(voucher)
    assert item.service_id is None


# ── Delivery Status Authorization Tests ─────────────────────────────────────────


def _make_jwt(claims: dict) -> str:
    import base64
    import json

    h = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    p = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{h}.{p}.fake_signature"


def _make_request(headers: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = Headers(headers or {})
    req.query_params = {}
    return req


def test_delivery_status_routes_registered():
    """Verify delivery-status routes are registered under both path prefixes."""
    paths = [route.path for route in app.routes]
    assert "/api/v1/vouchers/{voucher_id}/delivery-status/" in paths
    assert "/booknpay/api/v1/vouchers/{voucher_id}/delivery-status/" in paths


@pytest.mark.asyncio
async def test_employee_with_status_update_permission_can_update_delivery_status():
    """Verify employee with status_update permission can advance delivery status without secret_code."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="ordered")

    token = _make_jwt({
        "sub": str(uuid.uuid4()),
        "user_type": "employee",
        "permissions": ["status_update"],
    })
    req = _make_request({"Authorization": f"Bearer {token}"})

    with patch(
        "app.voucher.api.router.GiftVoucherService.update_delivery_status",
        new_callable=AsyncMock,
    ) as mock_update:
        voucher.delivery_status = "ready_to_go"
        mock_update.return_value = voucher

        body = UpdateVoucherDeliveryStatusRequest(delivery_status="ready_to_go")
        resp = await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
        )

        assert resp.status_code == 200
        mock_update.assert_awaited_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["voucher_id"] == voucher.id
        assert call_kwargs["new_status"] == "ready_to_go"
        assert "employee:" in call_kwargs["changed_by"]


@pytest.mark.asyncio
async def test_admin_employee_can_update_delivery_status():
    """Verify admin employee can advance delivery status without secret_code."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="ready_to_go")

    token = _make_jwt({
        "sub": str(uuid.uuid4()),
        "user_type": "admin",
    })
    req = _make_request({"Authorization": f"Bearer {token}"})

    with patch(
        "app.voucher.api.router.GiftVoucherService.update_delivery_status",
        new_callable=AsyncMock,
    ) as mock_update:
        voucher.delivery_status = "on_the_way"
        mock_update.return_value = voucher

        body = UpdateVoucherDeliveryStatusRequest(delivery_status="on_the_way")
        resp = await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
        )

        assert resp.status_code == 200
        mock_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_employee_via_custom_headers_can_update_delivery_status():
    """Verify employee via gateway headers (X-User-Type, X-Permissions) can update status."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="ordered")

    emp_id = str(uuid.uuid4())
    req = _make_request({
        "X-User-Type": "employee",
        "X-Permissions": "status_update",
        "X-Employee-Id": emp_id,
    })

    with patch(
        "app.voucher.api.router.GiftVoucherService.update_delivery_status",
        new_callable=AsyncMock,
    ) as mock_update:
        voucher.delivery_status = "ready_to_go"
        mock_update.return_value = voucher

        body = UpdateVoucherDeliveryStatusRequest(delivery_status="ready_to_go")
        resp = await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
        )

        assert resp.status_code == 200
        mock_update.assert_awaited_once()
        assert mock_update.call_args.kwargs["changed_by"] == f"employee:{emp_id}"


@pytest.mark.asyncio
async def test_employee_without_status_update_permission_rejected_without_secret_code():
    """Verify employee lacking status update permission is rejected if secret_code is absent."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="ordered")

    token = _make_jwt({
        "sub": str(uuid.uuid4()),
        "user_type": "employee",
        "permissions": ["view_only"],
    })
    req = _make_request({"Authorization": f"Bearer {token}", "X-USHSPA-TOKEN": "secret123"})

    body = UpdateVoucherDeliveryStatusRequest(delivery_status="ready_to_go")
    with pytest.raises(HTTPException) as exc_info:
        await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
        )

    assert exc_info.value.status_code == 403
    assert "Employee does not have status update permission" in exc_info.value.detail


@pytest.mark.asyncio
async def test_therapist_employee_rejected_without_secret_code():
    """Verify therapist employee is rejected without explicit status update permission or secret_code."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="ordered")

    token = _make_jwt({
        "sub": str(uuid.uuid4()),
        "user_type": "employee",
        "role": "Therapist",
    })
    req = _make_request({"Authorization": f"Bearer {token}", "X-USHSPA-TOKEN": "secret123"})

    body = UpdateVoucherDeliveryStatusRequest(delivery_status="ready_to_go")
    with pytest.raises(HTTPException) as exc_info:
        await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
        )

    assert exc_info.value.status_code == 403
    assert "Employee does not have status update permission" in exc_info.value.detail


@pytest.mark.asyncio
async def test_public_request_with_correct_secret_code_and_app_token_succeeds():
    """Verify public caller with X-USHSPA-TOKEN and matching secret_code in body succeeds."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="delivered")
    voucher.secret_code = "SEC-8899"

    req = _make_request({"X-USHSPA-TOKEN": "secret123"})
    settings = MagicMock()
    settings.USHSPA_TOKEN = "secret123"
    settings.USH_TOKEN = "secret123"

    with patch(
        "app.voucher.api.router.GiftVoucherService.get_by_id",
        new_callable=AsyncMock,
    ) as mock_get, patch(
        "app.voucher.api.router.GiftVoucherService.update_delivery_status",
        new_callable=AsyncMock,
    ) as mock_update:
        mock_get.return_value = voucher
        voucher.delivery_status = "received"
        mock_update.return_value = voucher

        body = UpdateVoucherDeliveryStatusRequest(
            delivery_status="received",
            secret_code="SEC-8899",
        )
        resp = await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
            settings=settings,
        )

        assert resp.status_code == 200
        mock_get.assert_awaited_once_with(voucher.id)
        mock_update.assert_awaited_once()
        assert "public" in mock_update.call_args.kwargs["changed_by"]


@pytest.mark.asyncio
async def test_public_request_with_wrong_secret_code_raises_400():
    """Verify public caller with wrong secret_code raises 400 Bad Request."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="delivered")
    voucher.secret_code = "SEC-8899"

    req = _make_request({"X-USHSPA-TOKEN": "secret123"})
    settings = MagicMock()
    settings.USHSPA_TOKEN = "secret123"
    settings.USH_TOKEN = "secret123"

    with patch(
        "app.voucher.api.router.GiftVoucherService.get_by_id",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = voucher

        body = UpdateVoucherDeliveryStatusRequest(
            delivery_status="received",
            secret_code="WRONG-CODE",
        )
        with pytest.raises(HTTPException) as exc_info:
            await update_voucher_delivery_status(
                voucher_id=voucher.id,
                body=body,
                session=mock_session,
                request=req,
                settings=settings,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid secret code."


@pytest.mark.asyncio
async def test_public_request_without_secret_code_raises_403():
    """Verify public caller with X-USHSPA-TOKEN but missing secret_code raises 403 Forbidden."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="delivered")

    req = _make_request({"X-USHSPA-TOKEN": "secret123"})
    settings = MagicMock()
    settings.USHSPA_TOKEN = "secret123"
    settings.USH_TOKEN = "secret123"

    body = UpdateVoucherDeliveryStatusRequest(delivery_status="received")
    with pytest.raises(HTTPException) as exc_info:
        await update_voucher_delivery_status(
            voucher_id=voucher.id,
            body=body,
            session=mock_session,
            request=req,
            settings=settings,
        )

    assert exc_info.value.status_code == 403
    assert "secret_code is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_request_without_app_token_and_without_employee_raises_401():
    """Verify request without app token and without employee auth raises 401."""
    mock_session = AsyncMock()
    voucher_id = uuid.uuid4()

    req = _make_request({})  # No headers
    body = UpdateVoucherDeliveryStatusRequest(delivery_status="ready_to_go")

    with pytest.raises(HTTPException) as exc_info:
        await update_voucher_delivery_status(
            voucher_id=voucher_id,
            body=body,
            session=mock_session,
            request=req,
        )

    assert exc_info.value.status_code == 401
    assert "header is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_delivery_status_voucher_not_found_raises_404():
    """Verify non-existent voucher raises 404."""
    mock_session = AsyncMock()
    voucher_id = uuid.uuid4()

    req = _make_request({"X-USHSPA-TOKEN": "secret123"})
    settings = MagicMock()
    settings.USHSPA_TOKEN = "secret123"
    settings.USH_TOKEN = "secret123"

    with patch(
        "app.voucher.api.router.GiftVoucherService.get_by_id",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.side_effect = NotFoundError(f"Gift voucher {voucher_id} not found.")

        body = UpdateVoucherDeliveryStatusRequest(
            delivery_status="received",
            secret_code="123456",
        )
        with pytest.raises(HTTPException) as exc_info:
            await update_voucher_delivery_status(
                voucher_id=voucher_id,
                body=body,
                session=mock_session,
                request=req,
                settings=settings,
            )

        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delivery_status_invalid_transition_raises_422():
    """Verify invalid state transition raises 422 Unprocessable Entity."""
    mock_session = AsyncMock()
    voucher = _make_mock_voucher(delivery_status="ordered")
    token = _make_jwt({
        "sub": str(uuid.uuid4()),
        "user_type": "employee",
        "permissions": ["status_update"],
    })
    req = _make_request({"Authorization": f"Bearer {token}"})

    with patch(
        "app.voucher.api.router.GiftVoucherService.update_delivery_status",
        new_callable=AsyncMock,
    ) as mock_update:
        mock_update.side_effect = ValidationError("Cannot transition from 'ordered' to 'received'.")

        body = UpdateVoucherDeliveryStatusRequest(delivery_status="received")
        with pytest.raises(HTTPException) as exc_info:
            await update_voucher_delivery_status(
                voucher_id=voucher.id,
                body=body,
                session=mock_session,
                request=req,
            )

        assert exc_info.value.status_code == 422


def test_delivery_status_http_accepts_flat_payload():
    """Verify HTTP endpoint accepts flat payload without 'body' wrapper."""
    from starlette.testclient import TestClient
    from app.core.database import get_db_session

    voucher_id = uuid.uuid4()
    mock_voucher = _make_mock_voucher(voucher_id=voucher_id, delivery_status="ready_to_go")

    mock_db = AsyncMock()
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        with patch(
            "app.voucher.api.router.GiftVoucherService.update_delivery_status",
            new_callable=AsyncMock,
        ) as mock_update:
            mock_update.return_value = mock_voucher

            client = TestClient(app, raise_server_exceptions=False)
            response = client.patch(
                f"/booknpay/api/v1/vouchers/{voucher_id}/delivery-status/",
                headers={
                    "X-User-Type": "employee",
                    "X-Permissions": "status_update",
                    "X-Employee-Id": str(uuid.uuid4()),
                },
                json={
                    "status": "",
                    "delivery_status": "ready_to_go",
                    "note": "Package is packed and ready",
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            mock_update.assert_awaited_once()
            call_kwargs = mock_update.call_args.kwargs
            assert call_kwargs["new_status"] == "ready_to_go"
            assert call_kwargs["note"] == "Package is packed and ready"
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def test_delivery_status_http_accepts_backward_compatible_body_payload():
    """Verify HTTP endpoint also accepts legacy nested 'body' payload."""
    from starlette.testclient import TestClient
    from app.core.database import get_db_session

    voucher_id = uuid.uuid4()
    mock_voucher = _make_mock_voucher(voucher_id=voucher_id, delivery_status="ready_to_go")

    mock_db = AsyncMock()
    app.dependency_overrides[get_db_session] = lambda: mock_db

    try:
        with patch(
            "app.voucher.api.router.GiftVoucherService.update_delivery_status",
            new_callable=AsyncMock,
        ) as mock_update:
            mock_update.return_value = mock_voucher

            client = TestClient(app, raise_server_exceptions=False)
            response = client.patch(
                f"/booknpay/api/v1/vouchers/{voucher_id}/delivery-status/",
                headers={
                    "X-User-Type": "employee",
                    "X-Permissions": "status_update",
                    "X-Employee-Id": str(uuid.uuid4()),
                },
                json={
                    "body": {
                        "status": "",
                        "delivery_status": "ready_to_go",
                        "note": "Package is packed and ready",
                    }
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            mock_update.assert_awaited_once()
            call_kwargs = mock_update.call_args.kwargs
            assert call_kwargs["new_status"] == "ready_to_go"
            assert call_kwargs["note"] == "Package is packed and ready"
    finally:
        app.dependency_overrides.pop(get_db_session, None)



