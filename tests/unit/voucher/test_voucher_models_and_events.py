"""
tests/unit/voucher/test_voucher_models_and_events.py
────────────────────────────────────────────────────
Unit tests for GiftVoucher models, schemas, and SQS event serialization
including payment_id (string), payment_data (dict), and payment_url (str).
"""

import json
import uuid
from decimal import Decimal

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
        )
        payload = json.loads(event.to_json())
        assert payload["payment_id"] == "100624710000000255"
        assert payload["payment_data"] == {"status": "paid"}
        assert payload["payment_url"] == "https://portal.myfatoorah.com/pay/123"
        # Verify renamed SQS event fields
        assert "sender_data" in payload
        assert "recipient_data" in payload
        assert "recipient_id" in payload
