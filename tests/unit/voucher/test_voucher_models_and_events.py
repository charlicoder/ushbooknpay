"""
tests/unit/voucher/test_voucher_models_and_events.py
────────────────────────────────────────────────────
Unit tests for GiftVoucher models, schemas, and SQS event serialization
including payment_id (string), payment_data (dict), and payment_url (str).
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.events.contracts import (
    VoucherActiveEvent,
    VoucherPaymentPendingEvent,
    VoucherRedeemedEvent,
)
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.interfaces.schemas import (
    CreateGiftVoucherRequest,
    GiftVoucherListItem,
    GiftVoucherResponse,
    UpdateGiftVoucherStatusRequest,
)


def test_gift_voucher_model_snapshot_with_payment_fields():
    """Verify to_snapshot includes payment_id, payment_data, and payment_url."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={"name": "Signature Massage"},
        total_amount=Decimal("45.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice", "phone_number": "+96512345678"},
        payment_id="100624710000000255",
        payment_data={"gateway": "MyFatoorah", "invoice_id": "12345"},
        payment_url="https://portal.myfatoorah.com/payment/12345",
    )
    snap = voucher.to_snapshot()
    assert snap["payment_id"] == "100624710000000255"
    assert snap["payment_data"] == {"gateway": "MyFatoorah", "invoice_id": "12345"}
    assert snap["payment_url"] == "https://portal.myfatoorah.com/payment/12345"
    # Verify renamed keys appear in snapshot
    assert "sender_data" in snap
    assert "recipient_data" in snap
    assert "recipient_id" in snap
    assert "price_for_extra_time" in snap
    assert snap["gift_category"] == "service"


def test_gift_voucher_model_snapshot_custom_category():
    """Verify to_snapshot includes custom gift_category."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        total_amount=Decimal("45.000"),
        sender_id=uuid.uuid4(),
        gift_category="digital",
    )
    snap = voucher.to_snapshot()
    assert snap["gift_category"] == "digital"


def test_create_voucher_schema_gift_category():
    """Verify CreateGiftVoucherRequest validates and defaults gift_category."""
    # Default
    req_default = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("20.000"),
    )
    assert req_default.gift_category == "service"

    # Explicit valid categories
    for cat in ("digital", "physical", "service", "DIGITAL", " PHYSICAL "):
        req = CreateGiftVoucherRequest(
            service_id=uuid.uuid4(),
            total_amount=Decimal("20.000"),
            gift_category=cat,
        )
        assert req.gift_category in ("digital", "physical", "service")

    # Invalid category
    with pytest.raises(ValueError, match="Invalid gift_category"):
        CreateGiftVoucherRequest(
            service_id=uuid.uuid4(),
            total_amount=Decimal("20.000"),
            gift_category="invalid_category",
        )


def test_gift_voucher_model_snapshot_new_fields():
    """Verify to_snapshot includes recipient_id and price_for_extra_time."""
    rec_id = uuid.uuid4()
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={},
        total_amount=Decimal("45.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
        recipient_id=rec_id,
        recipient_data={"name": "Bob"},
        price_for_extra_time=Decimal("5.000"),
    )
    snap = voucher.to_snapshot()
    assert snap["recipient_id"] == str(rec_id)
    assert snap["price_for_extra_time"] == "5.000"


def test_create_and_update_voucher_schemas_with_payment_url():
    """Verify schemas accept and serialize payment_url."""
    create_req = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("30.000"),
        payment_url="https://pay.example.com/checkout/123",
    )
    assert create_req.payment_url == "https://pay.example.com/checkout/123"

    update_req = UpdateGiftVoucherStatusRequest(
        status="payment_pending",
        payment_id="100624710000000255",
        payment_data={"status": "pending"},
        payment_url="https://pay.example.com/checkout/123",
    )
    assert update_req.payment_id == "100624710000000255"
    assert update_req.payment_url == "https://pay.example.com/checkout/123"


def test_create_voucher_schema_new_fields():
    """Verify CreateGiftVoucherRequest accepts recipient_id and price_for_extra_time."""
    rec_id = uuid.uuid4()
    req = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("45.000"),
        recipient_id=rec_id,
        price_for_extra_time=Decimal("5.000"),
    )
    assert req.recipient_id == rec_id
    assert req.price_for_extra_time == Decimal("5.000")
    # New field names should be present
    assert hasattr(req, "sender_data")
    assert hasattr(req, "recipient_data")


def test_create_voucher_request_with_sender_and_payment_data():
    """Verify CreateGiftVoucherRequest accepts sender_id, payment_data, payment_id, status, etc."""
    from datetime import datetime, timezone
    sender_id = uuid.uuid4()
    created_by = uuid.uuid4()
    req = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("50.000"),
        sender_id=sender_id,
        created_by=created_by,
        payment_id="100624710000000255",
        payment_data={"invoiceId": "100624710000000255", "provider": "myfatoorah"},
        payment_provider="MyFatoorah",
        payment_through="desk",
        status="active",
        expire_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )
    assert req.sender_id == sender_id
    assert req.created_by == created_by
    assert req.payment_id == "100624710000000255"
    assert req.payment_data == {"invoiceId": "100624710000000255", "provider": "myfatoorah"}
    assert req.payment_provider == "MyFatoorah"
    assert req.payment_through == "desk"
    assert req.status == "active"
    assert req.expire_date == datetime(2026, 12, 31, tzinfo=timezone.utc)


def test_voucher_response_schemas_with_payment_url():
    """Verify GiftVoucherResponse and ListItem have payment_url."""
    v_id = uuid.uuid4()
    s_id = uuid.uuid4()
    resp = GiftVoucherResponse(
        id=v_id,
        service_id=s_id,
        service_data={},
        branch_id=None,
        branch_data={},
        service_arrangement_id=None,
        service_arrangement_data={},
        addons=[],
        extra_time=0,
        expire_date="2026-11-01T00:00:00Z",
        status="active",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
        recipient_phone="+96599999999",
        recipient_data={},
        created_by=None,
        total_duration=60,
        total_amount="50.000",
        currency="KWD",
        gift_message=None,
        gift_template=None,
        secret_code="123456",
        public_token="token123",
        redeemed_booking_id=None,
        redeemed_at=None,
        booking_id=None,
        payment_id="100624710000000255",
        payment_data={"provider": "tap"},
        payment_url="https://checkout.tap.company/pay/123",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
    )
    assert resp.payment_url == "https://checkout.tap.company/pay/123"

    item = GiftVoucherListItem(
        id=v_id,
        service_id=s_id,
        service_data={},
        branch_id=None,
        status="active",
        total_amount="50.000",
        currency="KWD",
        expire_date="2026-11-01T00:00:00Z",
        recipient_phone="+96599999999",
        recipient_data={},
        public_token="token123",
        redeemed_at=None,
        payment_id="100624710000000255",
        payment_data={"provider": "tap"},
        payment_url="https://checkout.tap.company/pay/123",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
    )
    assert item.payment_url == "https://checkout.tap.company/pay/123"


def test_voucher_sqs_events_contain_payment_url():
    """Verify SQS event payloads serialize payment_url properly."""
    for event_cls in (VoucherActiveEvent, VoucherPaymentPendingEvent, VoucherRedeemedEvent):
        event = event_cls(
            id="vouch-123",
            payment_id="100624710000000255",
            payment_data={"status": "paid"},
            payment_url="https://portal.myfatoorah.com/pay/123",
            booking_data={"appointment_date": "2026-09-10", "booking_ref": "BK-999"},
        )
        payload = json.loads(event.to_json())
        assert payload["payment_id"] == "100624710000000255"
        assert payload["payment_data"] == {"status": "paid"}
        assert payload["payment_url"] == "https://portal.myfatoorah.com/pay/123"
        assert payload["booking_data"] == {"appointment_date": "2026-09-10", "booking_ref": "BK-999"}
        # Verify renamed SQS event fields
        assert "sender_data" in payload
        assert "recipient_data" in payload
        assert "recipient_id" in payload


def test_gift_voucher_model_snapshot_with_booking_data():
    """Verify to_snapshot includes booking_data dictionary."""
    b_data = {"appointment_date": "2026-09-10", "branch_name": "Salmiya"}
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={},
        total_amount=Decimal("45.000"),
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
        booking_data=b_data,
    )
    snap = voucher.to_snapshot()
    assert snap["booking_data"] == b_data


def test_create_and_update_voucher_schemas_with_booking_data():
    """Verify schemas accept and serialize booking_data."""
    b_id = uuid.uuid4()
    b_data = {"booking_ref": "BK-12345", "appointment_date": "2026-09-15"}

    create_req = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("45.000"),
        booking_id=b_id,
        booking_data=b_data,
    )
    assert create_req.booking_id == b_id
    assert create_req.booking_data == b_data

    update_req = UpdateGiftVoucherStatusRequest(
        status="redeemed",
        booking_id=b_id,
        booking_data=b_data,
    )
    assert update_req.booking_id == b_id
    assert update_req.booking_data == b_data

    # Response schema
    resp = GiftVoucherResponse(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={},
        branch_id=None,
        branch_data={},
        service_arrangement_id=None,
        service_arrangement_data={},
        addons=[],
        extra_time=0,
        expire_date="2026-11-01T00:00:00Z",
        status="active",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Alice"},
        recipient_phone="+96599999999",
        recipient_data={},
        created_by=None,
        total_duration=60,
        total_amount="50.000",
        currency="KWD",
        gift_message=None,
        gift_template=None,
        secret_code="123456",
        public_token="token123",
        redeemed_booking_id=None,
        redeemed_at=None,
        booking_id=b_id,
        booking_data=b_data,
        payment_id="100624710000000255",
        payment_data={"provider": "tap"},
        payment_url="https://checkout.tap.company/pay/123",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
    )
    assert resp.booking_id == b_id
    assert resp.booking_data == b_data

    # List item schema
    item = GiftVoucherListItem(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={},
        branch_id=None,
        status="active",
        total_amount="50.000",
        currency="KWD",
        expire_date="2026-11-01T00:00:00Z",
        recipient_phone="+96599999999",
        recipient_data={},
        public_token="token123",
        redeemed_at=None,
        booking_id=b_id,
        booking_data=b_data,
        payment_id="100624710000000255",
        payment_data={"provider": "tap"},
        payment_url="https://checkout.tap.company/pay/123",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
    )
    assert item.booking_id == b_id
    assert item.booking_data == b_data


def test_gift_voucher_snapshot_and_schemas_include_redeemed_by_and_created_by():
    """Verify to_snapshot, GiftVoucherResponse, and GiftVoucherListItem include redeemed_by and created_by."""
    v_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    redeemer_id = uuid.uuid4()
    voucher = GiftVoucher(
        id=v_id,
        service_id=uuid.uuid4(),
        service_data={"name": "Signature Massage"},
        total_amount=Decimal("45.000"),
        sender_id=uuid.uuid4(),
        created_by=creator_id,
        redeemed_by=redeemer_id,
        redeemed_at=datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc),
    )
    snap = voucher.to_snapshot()
    assert snap["created_by"] == str(creator_id)
    assert snap["redeemed_by"] == str(redeemer_id)
    assert snap["redeemed_at"] is not None

    resp = GiftVoucherResponse(
        id=v_id,
        service_id=uuid.uuid4(),
        service_data={},
        branch_id=None,
        branch_data={},
        service_arrangement_id=None,
        service_arrangement_data={},
        addons=[],
        extra_time=0,
        expire_date="2026-11-01T00:00:00Z",
        status="redeemed",
        sender_id=uuid.uuid4(),
        sender_data={},
        recipient_phone=None,
        recipient_data={},
        created_by=creator_id,
        redeemed_by=redeemer_id,
        redeemed_at="2026-09-10T15:00:00Z",
        total_duration=60,
        total_amount="45.000",
        currency="KWD",
        gift_message=None,
        gift_template=None,
        secret_code="123456",
        public_token="token123",
        redeemed_booking_id=None,
        booking_id=None,
        payment_id=None,
        payment_data=None,
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-10T15:00:00Z",
    )
    assert resp.created_by == creator_id
    assert resp.redeemed_by == redeemer_id
    assert resp.redeemed_at is not None

    item = GiftVoucherListItem(
        id=v_id,
        service_id=uuid.uuid4(),
        service_data={},
        branch_id=None,
        status="redeemed",
        total_amount="45.000",
        currency="KWD",
        expire_date="2026-11-01T00:00:00Z",
        recipient_phone=None,
        recipient_data={},
        public_token="token123",
        created_by=creator_id,
        redeemed_by=redeemer_id,
        redeemed_at="2026-09-10T15:00:00Z",
        payment_id=None,
        payment_data=None,
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-10T15:00:00Z",
    )
    assert item.created_by == creator_id
    assert item.redeemed_by == redeemer_id


@pytest.mark.asyncio
async def test_update_status_redeemed_auto_updates_redeemed_at_and_redeemed_by():
    """When transitioning to redeemed, redeemed_at and redeemed_by are automatically set."""
    from unittest.mock import AsyncMock, MagicMock
    from app.voucher.application.voucher_service import GiftVoucherService

    mock_session = AsyncMock()
    svc = GiftVoucherService(mock_session)

    v_id = uuid.uuid4()
    voucher = GiftVoucher(
        id=v_id,
        service_id=uuid.uuid4(),
        service_data={},
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        status="active",
        created_by=uuid.uuid4(),
    )
    assert voucher.redeemed_at is None
    assert voucher.redeemed_by is None

    svc._repo.get_by_id = AsyncMock(return_value=voucher)
    svc._repo.flush = AsyncMock()

    api_requester_id = uuid.uuid4()
    updated = await svc.update_status(
        voucher_id=v_id,
        new_status="redeemed",
        redeemed_by=api_requester_id,
    )

    assert updated.status == "redeemed"
    assert updated.redeemed_at is not None
    assert updated.redeemed_by == api_requester_id


def test_create_voucher_empty_booking_id_and_lowercase_payment_provider():
    """Verify that empty string booking_id coerces to None and lowercase payment_provider normalises."""
    req = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("45.000"),
        booking_id="",
        payment_provider="directlink",
        payment_through="ushspa",
    )
    assert req.booking_id is None
    assert req.payment_provider == "DirectLink"
    assert req.payment_through == "ushspa"


def test_create_voucher_empty_string_coercion_across_optional_fields():
    """Verify empty strings across all optional fields coerce cleanly to None."""
    req = CreateGiftVoucherRequest(
        service_id=uuid.uuid4(),
        total_amount=Decimal("45.000"),
        branch_id="",
        service_arrangement_id="",
        recipient_id="",
        sender_id="",
        created_by="",
        booking_id="",
        booking_data="",
        payment_data="",
        payment_id="",
        payment_url="",
        gift_message="",
        gift_template="",
        recipient_phone="",
        expire_date="",
        status="",
        payment_provider="",
        payment_through="",
    )
    assert req.branch_id is None
    assert req.service_arrangement_id is None
    assert req.recipient_id is None
    assert req.sender_id is None
    assert req.created_by is None
    assert req.booking_id is None
    assert req.booking_data is None
    assert req.payment_data is None
    assert req.payment_id is None
    assert req.payment_url is None
    assert req.gift_message is None
    assert req.gift_template is None
    assert req.recipient_phone is None
    assert req.expire_date is None
    assert req.status is None
    assert req.payment_provider is None
    assert req.payment_through is None


def test_payment_provider_and_through_normalisation():
    """Verify provider and channel case-insensitivity and alias normalization."""
    from app.voucher.domain.value_objects import VoucherPaymentProvider, VoucherPaymentThrough

    assert VoucherPaymentProvider.normalise("directlink") == "DirectLink"
    assert VoucherPaymentProvider.normalise("DIRECTLINK") == "DirectLink"
    assert VoucherPaymentProvider.normalise("direct_link") == "DirectLink"
    assert VoucherPaymentProvider.normalise("direct-link") == "DirectLink"
    assert VoucherPaymentProvider.normalise("myfatoorah") == "MyFatoorah"
    assert VoucherPaymentProvider.normalise("myfatora") == "MyFatoorah"
    assert VoucherPaymentProvider.normalise("fatoorah") == "MyFatoorah"
    assert VoucherPaymentProvider.normalise("deema") == "Deema"
    assert VoucherPaymentProvider.normalise("other") == "Other"
    assert VoucherPaymentProvider.normalise("") is None
    assert VoucherPaymentProvider.normalise(None) is None

    assert VoucherPaymentThrough.normalise("ushspa") == "ushspa"
    assert VoucherPaymentThrough.normalise("USHSPA") == "ushspa"
    assert VoucherPaymentThrough.normalise("app") == "ushspa"
    assert VoucherPaymentThrough.normalise("web") == "ushspa"
    assert VoucherPaymentThrough.normalise("desk") == "desk"
    assert VoucherPaymentThrough.normalise("DESK") == "desk"
    assert VoucherPaymentThrough.normalise("pos") == "desk"
    assert VoucherPaymentThrough.normalise("reception") == "desk"
    assert VoucherPaymentThrough.normalise("") is None
    assert VoucherPaymentThrough.normalise(None) is None


def test_update_voucher_status_empty_strings_and_normalisation():
    """Verify UpdateGiftVoucherStatusRequest handles empty strings and normalises provider."""
    req = UpdateGiftVoucherStatusRequest(
        status="ACTIVE",
        booking_id="",
        redeemed_by="",
        payment_id="",
        payment_url="",
        payment_data="",
        payment_provider="directlink",
        payment_through="desk",
    )
    assert req.status == "active"
    assert req.booking_id is None
    assert req.redeemed_by is None
    assert req.payment_id is None
    assert req.payment_url is None
    assert req.payment_data is None
    assert req.payment_provider == "DirectLink"
    assert req.payment_through == "desk"


def test_validation_exception_handler_serializes_value_error_without_crashing():
    """Verify validation errors containing ValueError in ctx are serialized to JSON without TypeError."""
    from starlette.testclient import TestClient
    from app.main import app
    from app.core.security import TokenPayload, require_authenticated_user

    app.dependency_overrides[require_authenticated_user] = lambda: TokenPayload(sub=str(uuid.uuid4()))
    try:
        client = TestClient(app, raise_server_exceptions=False)
        # Send a request with an invalid payment_provider to trigger ValueError in validator
        resp = client.post(
            "/api/v1/vouchers/",
            json={
                "service_id": str(uuid.uuid4()),
                "total_amount": "45.000",
                "payment_provider": "unsupported_gateway",
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid payment_provider" in str(data["error"]["detail"])
    finally:
        app.dependency_overrides.pop(require_authenticated_user, None)


def test_gift_voucher_model_snapshot_with_delivery_fields():
    """Verify to_snapshot includes ordered_items, delivery_status, and delivery_address."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        gift_category="physical",
        ordered_items=[{"item_id": "item-123", "name": "Spa Bathrobe", "quantity": 1, "price": 50.0}],
        delivery_status="ordered",
        delivery_address={"block": "1", "street": "Gulf Road", "city": "Kuwait City"},
    )
    snap = voucher.to_snapshot()
    assert snap["gift_category"] == "physical"
    assert snap["ordered_items"] == [{"item_id": "item-123", "name": "Spa Bathrobe", "quantity": 1, "price": 50.0}]
    assert snap["delivery_status"] == "ordered"
    assert snap["delivery_address"] == {"block": "1", "street": "Gulf Road", "city": "Kuwait City"}


def test_events_contract_with_delivery_fields_unpacking():
    """Verify SQS events can be unpacked cleanly from a snapshot containing delivery fields."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={"name": "Massage"},
        total_amount=Decimal("30.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender"},
        recipient_phone="+96599998888",
        recipient_id=uuid.uuid4(),
        recipient_data={"name": "Recipient"},
        secret_code="SEC999",
        public_token="pub999",
        gift_category="physical",
        ordered_items=[{"item_id": "p-1"}],
        delivery_status="ordered",
        delivery_address={"area": "Salmiya"},
        status="active",
    )
    snap = voucher.to_snapshot()

    # Active Event
    active_ev = VoucherActiveEvent(**snap)
    assert active_ev.gift_category == "physical"
    assert active_ev.ordered_items == [{"item_id": "p-1"}]
    assert active_ev.delivery_status == "ordered"
    assert active_ev.delivery_address == {"area": "Salmiya"}

    # Payment Pending Event
    pending_ev = VoucherPaymentPendingEvent(**snap)
    assert pending_ev.gift_category == "physical"
    assert pending_ev.ordered_items == [{"item_id": "p-1"}]
    assert pending_ev.delivery_status == "ordered"
    assert pending_ev.delivery_address == {"area": "Salmiya"}

    # Redeemed Event
    redeemed_ev = VoucherRedeemedEvent(**snap)
    assert redeemed_ev.gift_category == "physical"
    assert redeemed_ev.ordered_items == [{"item_id": "p-1"}]
    assert redeemed_ev.delivery_status == "ordered"
    assert redeemed_ev.delivery_address == {"area": "Salmiya"}


def test_update_voucher_delivery_status_schema():
    """Verify UpdateVoucherDeliveryStatusRequest validation."""
    from app.voucher.interfaces.schemas import UpdateVoucherDeliveryStatusRequest

    # Valid values
    r1 = UpdateVoucherDeliveryStatusRequest(delivery_status="ready_to_go")
    assert r1.delivery_status == "ready_to_go"
    assert r1.status == "ready_to_go"

    r2 = UpdateVoucherDeliveryStatusRequest(delivery_status="READY_TO_GO")
    assert r2.delivery_status == "ready_to_go"
    assert r2.status == "ready_to_go"

    r3 = UpdateVoucherDeliveryStatusRequest(delivery_status="on_the_way")
    assert r3.delivery_status == "on_the_way"

    # Status empty string with valid delivery_status (as in Swagger UI)
    r4 = UpdateVoucherDeliveryStatusRequest.model_validate({
        "status": "",
        "delivery_status": "ready_to_go",
        "note": "Package ready",
    })
    assert r4.status == "ready_to_go"
    assert r4.delivery_status == "ready_to_go"
    assert r4.note == "Package ready"

    # Status provided, delivery_status empty or None
    r5 = UpdateVoucherDeliveryStatusRequest.model_validate({
        "status": "delivered",
        "delivery_status": "",
        "note": "Delivered to reception",
    })
    assert r5.status == "delivered"
    assert r5.delivery_status == "delivered"

    # Backward-compatible nested body unwrap
    r6 = UpdateVoucherDeliveryStatusRequest.model_validate({
        "body": {
            "status": "",
            "delivery_status": "received",
            "note": "Customer received",
        }
    })
    assert r6.status == "received"
    assert r6.delivery_status == "received"
    assert r6.note == "Customer received"

    # Invalid value
    with pytest.raises(ValueError, match="Invalid delivery_status"):
        UpdateVoucherDeliveryStatusRequest(delivery_status="shipped_somewhere")


def test_delivery_state_machine_transitions():
    """Verify DeliveryStateMachine transitions for vouchers."""
    from app.shop.domain.state_machine import (
        DeliveryStateMachine,
        InvalidDeliveryTransitionError,
    )
    from app.shop.domain.value_objects import DeliveryStatus

    # Valid transitions: ordered -> ready_to_go -> on_the_way -> delivered -> received
    sm_ordered = DeliveryStateMachine(DeliveryStatus.ORDERED)
    assert sm_ordered.can_transition(DeliveryStatus.READY_TO_GO)
    sm_ordered.assert_can_transition(DeliveryStatus.READY_TO_GO)

    sm_ready = DeliveryStateMachine(DeliveryStatus.READY_TO_GO)
    assert sm_ready.can_transition(DeliveryStatus.ON_THE_WAY)
    sm_ready.assert_can_transition(DeliveryStatus.ON_THE_WAY)

    sm_otw = DeliveryStateMachine(DeliveryStatus.ON_THE_WAY)
    assert sm_otw.can_transition(DeliveryStatus.DELIVERED)
    sm_otw.assert_can_transition(DeliveryStatus.DELIVERED)

    sm_del = DeliveryStateMachine(DeliveryStatus.DELIVERED)
    assert sm_del.can_transition(DeliveryStatus.RECEIVED)
    sm_del.assert_can_transition(DeliveryStatus.RECEIVED)

    # Invalid transition directly: ordered -> delivered
    assert not sm_ordered.can_transition(DeliveryStatus.DELIVERED)
    with pytest.raises(InvalidDeliveryTransitionError):
        sm_ordered.assert_can_transition(DeliveryStatus.DELIVERED)


def test_router_delivery_labels_mapping():
    """Verify router response serialization includes bilingual delivery labels."""
    from app.voucher.api.router import _voucher_to_response, _voucher_to_public, _voucher_to_list_item

    now = datetime.now(timezone.utc)
    v = GiftVoucher(
        id=uuid.uuid4(),
        service_id=uuid.uuid4(),
        service_data={"name": "Aromatherapy"},
        total_amount=Decimal("40.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender User"},
        gift_category="physical",
        ordered_items=[{"name": "Oil", "qty": 1}],
        delivery_status="ready_to_go",
        delivery_address={"block": "2", "street": "Street 10"},
        status="active",
        secret_code="SEC888",
        public_token="PUB888",
        extra_time=0,
        total_duration=60,
        expire_date=now,
        created_at=now,
        updated_at=now,
    )

    resp = _voucher_to_response(v)
    assert resp.delivery_status == "ready_to_go"
    assert resp.delivery_status_label == "Ready To Go"
    assert resp.delivery_status_label_ar == "جاهز للإرسال"
    assert resp.ordered_items == [{"name": "Oil", "qty": 1}]
    assert resp.delivery_address == {"block": "2", "street": "Street 10"}

    pub = _voucher_to_public(v)
    assert pub.delivery_status == "ready_to_go"
    assert pub.delivery_status_label == "Ready To Go"
    assert pub.delivery_status_label_ar == "جاهز للإرسال"
    assert pub.ordered_items == [{"name": "Oil", "qty": 1}]
    assert pub.delivery_address == {"block": "2", "street": "Street 10"}

    item = _voucher_to_list_item(v)
    assert item.delivery_status == "ready_to_go"
    assert item.delivery_status_label == "Ready To Go"
    assert item.delivery_status_label_ar == "جاهز للإرسال"
    assert item.ordered_items == [{"name": "Oil", "qty": 1}]
    assert item.delivery_address == {"block": "2", "street": "Street 10"}


def test_digital_product_data_and_is_digital_gift_opened_snapshot():
    """Verify to_snapshot includes digital_product_data and is_digital_gift_opened."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=None,
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
        gift_category="digital",
        digital_product_data={"download_url": "https://example.com/item.pdf", "code": "DIGI-999"},
        is_digital_gift_opened=True,
    )
    snap = voucher.to_snapshot()
    assert snap["digital_product_data"] == {"download_url": "https://example.com/item.pdf", "code": "DIGI-999"}
    assert snap["is_digital_gift_opened"] is True


def test_digital_product_data_defaults():
    """Verify default values for digital_product_data and is_digital_gift_opened."""
    voucher = GiftVoucher(
        id=uuid.uuid4(),
        service_id=None,
        total_amount=Decimal("50.000"),
        sender_id=uuid.uuid4(),
    )
    assert voucher.digital_product_data is None
    snap = voucher.to_snapshot()
    assert snap["digital_product_data"] == {}
    assert snap["is_digital_gift_opened"] is False


def test_digital_product_data_and_is_opened_in_events():
    """Verify domain events serialize digital_product_data and is_digital_gift_opened."""
    vid = str(uuid.uuid4())
    digital_data = {"tier": "gold", "features": ["spa", "sauna"]}

    event_active = VoucherActiveEvent(
        id=vid,
        sender_data={"name": "Sender", "phone_number": "+96512345678"},
        recipient_phone="+96587654321",
        secret_code="123456",
        public_token="tok123",
        expire_date=datetime.now(timezone.utc).isoformat(),
        total_amount="50.000",
        currency="KWD",
        digital_product_data=digital_data,
        is_digital_gift_opened=True,
    )
    payload_active = event_active.to_dict()
    assert payload_active["digital_product_data"] == digital_data
    assert payload_active["is_digital_gift_opened"] is True

    event_pending = VoucherPaymentPendingEvent(
        id=vid,
        total_amount="50.000",
        currency="KWD",
        digital_product_data=digital_data,
        is_digital_gift_opened=False,
    )
    payload_pending = event_pending.to_dict()
    assert payload_pending["digital_product_data"] == digital_data
    assert payload_pending["is_digital_gift_opened"] is False

    event_redeemed = VoucherRedeemedEvent(
        id=vid,
        recipient_phone="+96587654321",
        redeemed_by=str(uuid.uuid4()),
        redeemed_at=datetime.now(timezone.utc).isoformat(),
        digital_product_data=digital_data,
        is_digital_gift_opened=True,
    )
    payload_redeemed = event_redeemed.to_dict()
    assert payload_redeemed["digital_product_data"] == digital_data
    assert payload_redeemed["is_digital_gift_opened"] is True


def test_router_digital_product_data_serialization():
    """Verify router serialization includes digital_product_data and is_digital_gift_opened."""
    from app.voucher.api.router import _voucher_to_response, _voucher_to_public, _voucher_to_list_item

    now = datetime.now(timezone.utc)
    digital_data = {"download_link": "https://example.com/asset.zip", "key": "ABC-DEF"}
    v = GiftVoucher(
        id=uuid.uuid4(),
        service_id=None,
        total_amount=Decimal("60.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender"},
        gift_category="digital",
        digital_product_data=digital_data,
        is_digital_gift_opened=True,
        status="active",
        secret_code="SEC999",
        public_token="PUB999",
        extra_time=0,
        total_duration=0,
        expire_date=now,
        created_at=now,
        updated_at=now,
    )

    resp = _voucher_to_response(v)
    assert resp.digital_product_data == digital_data
    assert resp.is_digital_gift_opened is True

    pub = _voucher_to_public(v)
    assert pub.digital_product_data == digital_data
    assert pub.is_digital_gift_opened is True

    item = _voucher_to_list_item(v)
    assert item.digital_product_data == digital_data
    assert item.is_digital_gift_opened is True


def test_voucher_gift_from_support():
    """Verify gift_from field is supported across models, schemas, serializers, and events."""
    from app.events.contracts import (
        VoucherActiveEvent,
        VoucherPaymentPendingEvent,
        VoucherRedeemedEvent,
    )
    from app.voucher.api.router import (
        _voucher_to_list_item,
        _voucher_to_public,
        _voucher_to_response,
    )
    from app.voucher.interfaces.schemas import (
        CreateGiftVoucherRequest,
        UpdateGiftVoucherRequest,
    )

    # 1. Model & to_snapshot()
    now = datetime.now(timezone.utc)
    v = GiftVoucher(
        id=uuid.uuid4(),
        service_id=None,
        total_amount=Decimal("45.000"),
        currency="KWD",
        sender_id=uuid.uuid4(),
        sender_data={"name": "Sender"},
        gift_message="Happy Birthday!",
        gift_from="Your Friend Sarah",
        status="active",
        secret_code="SEC123",
        public_token="PUB123",
        extra_time=0,
        total_duration=0,
        expire_date=now,
        created_at=now,
        updated_at=now,
    )
    assert v.gift_from == "Your Friend Sarah"
    snapshot = v.to_snapshot()
    assert snapshot["gift_from"] == "Your Friend Sarah"

    # 2. SQS events
    active_ev = VoucherActiveEvent(**snapshot)
    assert active_ev.gift_from == "Your Friend Sarah"

    pending_ev = VoucherPaymentPendingEvent(**snapshot)
    assert pending_ev.gift_from == "Your Friend Sarah"

    redeemed_ev = VoucherRedeemedEvent(**snapshot)
    assert redeemed_ev.gift_from == "Your Friend Sarah"

    # 3. Router serializers
    resp = _voucher_to_response(v)
    assert resp.gift_from == "Your Friend Sarah"

    pub = _voucher_to_public(v)
    assert pub.gift_from == "Your Friend Sarah"

    item = _voucher_to_list_item(v)
    assert item.gift_from == "Your Friend Sarah"

    # 4. Request schemas
    req = CreateGiftVoucherRequest(
        total_amount=Decimal("45.000"),
        gift_from="Your Friend Sarah",
    )
    assert req.gift_from == "Your Friend Sarah"

    # Empty string coerced to None
    req_empty = CreateGiftVoucherRequest(
        total_amount=Decimal("45.000"),
        gift_from="   ",
    )
    assert req_empty.gift_from is None

    update_req = UpdateGiftVoucherRequest(
        gift_from="Aunt Emily",
    )
    assert update_req.gift_from == "Aunt Emily"






