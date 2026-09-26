"""
tests/unit/shop/test_shop_order_payment_through_and_requested_by.py
───────────────────────────────────────────────────────────────────
Unit tests for shop_orders updates:
1. payment_through (ushspa, ushdesk, other) and normalisation.
2. order_requested_by_user (logged-in user_id) and order_requested_by_user_data (JSON snapshot).
3. Backwards compatibility for payment_type.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.security import TokenPayload
from app.events.contracts import ShopOrderCreatedEvent
from app.shop.domain.value_objects import DeliveryStatus, OrderPaymentStatus, ShopPaymentThrough
from app.shop.infrastructure.models import ShopOrder
from app.shop.interfaces.schemas import (
    CreateShopOrderRequest,
    OrderItemIn,
    ShopOrderDetailResponse,
    ShopOrderListItem,
    UpdatePaymentStatusRequest,
    order_to_detail,
    order_to_list_item,
)
from app.shop.application.services import ShopOrderService


def test_shop_payment_through_enum_values():
    """Verify ShopPaymentThrough enum values and normalise helper."""
    assert ShopPaymentThrough.USHSPA.value == "ushspa"
    assert ShopPaymentThrough.USHDESK.value == "ushdesk"
    assert ShopPaymentThrough.OTHER.value == "other"

    assert ShopPaymentThrough.normalise("ushspa") == "ushspa"
    assert ShopPaymentThrough.normalise("app") == "ushspa"
    assert ShopPaymentThrough.normalise("mobile") == "ushspa"
    assert ShopPaymentThrough.normalise("ushdesk") == "ushdesk"
    assert ShopPaymentThrough.normalise("desk") == "ushdesk"
    assert ShopPaymentThrough.normalise("pos") == "ushdesk"
    assert ShopPaymentThrough.normalise("other") == "other"
    assert ShopPaymentThrough.normalise("anything_else") == "other"
    assert ShopPaymentThrough.normalise(None) is None
    assert ShopPaymentThrough.normalise("") is None


def test_shop_order_model_fields_and_compat():
    """Verify ShopOrder model has payment_through, order_requested_by_user, payment_url, payment_data, and compat."""
    order_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user_data = {"id": str(user_id), "name": "Jane Doe", "image": "https://example.com/avatar.jpg"}
    pay_data = {"invoice_id": "inv_12345", "gateway": "MyFatoorah"}

    order = ShopOrder(
        id=order_id,
        order_number="ORD-260925001",
        customer_id=cust_id,
        customer_name="Jane Doe",
        customer_phone="+96599999999",
        order_requested_by_user=user_id,
        order_requested_by_user_data=user_data,
        payment_through=ShopPaymentThrough.USHSPA.value,
        payment_url="https://pay.example.com/invoice/12345",
        payment_data=pay_data,
        total_amount=Decimal("15.000"),
        tracking_code="123456",
        public_token="token_xyz",
    )

    assert order.order_requested_by_user == user_id
    assert order.order_requested_by_user_data == user_data
    assert order.payment_through == "ushspa"
    assert order.payment_url == "https://pay.example.com/invoice/12345"
    assert order.payment_data == pay_data
    # Backwards-compatibility property
    assert order.payment_type == "ushspa"

    order.payment_type = "ushdesk"
    assert order.payment_through == "ushdesk"
    assert order.payment_type == "ushdesk"


def test_create_shop_order_request_schema():
    """Verify CreateShopOrderRequest accepts requester, payment, and payment_url/payment_data fields."""
    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    user_data = {"id": str(user_id), "name": "Test User", "image": "img.png"}
    pay_data = {"gateway": "knet", "invoice": 123}

    # Explicit payment_through, payment_url, and payment_data
    req = CreateShopOrderRequest(
        items=[OrderItemIn(product_id=item_id, quantity=1)],
        order_requested_by_user=user_id,
        order_requested_by_user_data=user_data,
        payment_through="ushspa",
        payment_url="https://pay.example.com/checkout",
        payment_data=pay_data,
    )
    assert req.order_requested_by_user == user_id
    assert req.order_requested_by_user_data == user_data
    assert req.payment_through == "ushspa"
    assert req.payment_url == "https://pay.example.com/checkout"
    assert req.payment_data == pay_data

    # Deprecated payment_type synced to payment_through
    req2 = CreateShopOrderRequest(
        items=[OrderItemIn(product_id=item_id, quantity=1)],
        payment_type="ushdesk",
    )
    assert req2.payment_through == "ushdesk"


def test_update_payment_status_request_schema():
    """Verify UpdatePaymentStatusRequest accepts payment_through, payment_url, payment_data, and syncs payment_type."""
    pay_data = {"receipt_id": "rec_999"}
    req = UpdatePaymentStatusRequest(
        payment_status=OrderPaymentStatus.SUCCESS,
        payment_through="ushdesk",
        payment_method="cash",
        payment_url="https://pay.example.com/status",
        payment_data=pay_data,
    )
    assert req.payment_through == "ushdesk"
    assert req.payment_url == "https://pay.example.com/status"
    assert req.payment_data == pay_data

    # Fallback from payment_type
    req2 = UpdatePaymentStatusRequest(
        payment_status=OrderPaymentStatus.SUCCESS,
        payment_type="ushspa",
    )
    assert req2.payment_through == "ushspa"


def test_order_to_detail_and_list_item():
    """Verify order_to_detail and order_to_list_item map new fields."""
    order = MagicMock(spec=ShopOrder)
    order.id = uuid.uuid4()
    order.order_number = "ORD-260925001"
    order.customer_id = uuid.uuid4()
    order.customer_name = "Jane Doe"
    order.customer_phone = "+96511111111"
    order.contact_number = "+96511111111"
    order.delivery_status = "ordered"
    order.payment_status = "success"
    order.payment_through = "ushspa"
    order.payment_method = "card"
    order.payment_provider = "MyFatoorah"
    order.payment_url = "https://pay.example.com/123"
    order.payment_data = {"auth_code": "XYZ789"}
    user_id = uuid.uuid4()
    order.order_requested_by_user = user_id
    order.order_requested_by_user_data = {"id": str(user_id), "name": "Jane", "image": "pic.jpg"}
    order.area = "Salmiya"
    order.block = "1"
    order.street = "Salem Al-Mubarak"
    order.building_no = "12"
    order.floor = "2"
    order.apartment = "4"
    order.city = "Hawally"
    order.formatted_address = "Area: Salmiya, Block: 1"
    order.delivery_notes = None
    order.subtotal = Decimal("10.000")
    order.discount = Decimal("0.000")
    order.total_amount = Decimal("10.000")
    order.currency = "KWD"
    order.internal_notes = None
    order.items = []
    order.status_history = []
    order.public_token = "pub_tok"
    order.token_expires_at = None
    now = datetime.now(timezone.utc)
    order.created_at = now
    order.updated_at = now

    with patch("app.shop.interfaces.schemas._build_tracking_url", return_value="http://track"):
        detail = order_to_detail(order)
        assert detail.payment_through == "ushspa"
        assert detail.payment_method == "card"
        assert detail.payment_url == "https://pay.example.com/123"
        assert detail.payment_data == {"auth_code": "XYZ789"}
        assert detail.order_requested_by_user == user_id
        assert detail.order_requested_by_user_data["image"] == "pic.jpg"

        list_item = order_to_list_item(order)
        assert list_item.payment_through == "ushspa"
        assert list_item.payment_url == "https://pay.example.com/123"
        assert list_item.payment_data == {"auth_code": "XYZ789"}
        assert list_item.order_requested_by_user == user_id
        assert list_item.order_requested_by_user_data["id"] == str(user_id)


@pytest.mark.asyncio
async def test_service_create_order_persists_requested_by_and_payment_through():
    """Verify ShopOrderService.create_order persists requester, payment_through, payment_url, and payment_data."""
    session = AsyncMock()
    repo_mock = AsyncMock()
    repo_mock.next_order_number.return_value = "ORD-260925001"
    created_order = None

    async def fake_create(ord_obj):
        nonlocal created_order
        created_order = ord_obj
        return ord_obj

    repo_mock.create.side_effect = fake_create

    ushauth_client = AsyncMock()
    prod_id = uuid.uuid4()
    ushauth_client.get_product.return_value = {
        "id": str(prod_id),
        "name": "Argan Oil",
        "price": "12.500",
        "image1": "https://example.com/oil.jpg",
    }

    svc = ShopOrderService(session=session, ushauth_client=ushauth_client)
    svc._repo = repo_mock

    user_id = uuid.uuid4()
    user_data = {"id": str(user_id), "name": "Customer One", "image": "cust.jpg"}
    pay_data = {"invoice_id": "inv_999"}
    req = CreateShopOrderRequest(
        items=[OrderItemIn(product_id=prod_id, quantity=2)],
        area="Shuwaikh",
        block="2",
        street="Gulf St",
        building_no="5",
        order_requested_by_user=user_id,
        order_requested_by_user_data=user_data,
        payment_through="ushspa",
        payment_url="https://pay.example.com/pay_link",
        payment_data=pay_data,
    )

    cust_id = uuid.uuid4()
    order = await svc.create_order(
        body=req,
        customer_id=cust_id,
        customer_name="Customer One",
        customer_phone="+96590000000",
    )

    assert created_order is not None
    assert created_order.order_requested_by_user == user_id
    assert created_order.order_requested_by_user_data == user_data
    assert created_order.payment_through == "ushspa"
    assert created_order.payment_url == "https://pay.example.com/pay_link"
    assert created_order.payment_data == pay_data


@pytest.mark.asyncio
async def test_service_update_payment_status_normalises_payment_through():
    """Verify ShopOrderService.update_payment_status updates payment_through, payment_url, and payment_data."""
    session = AsyncMock()
    repo_mock = AsyncMock()

    order = MagicMock(spec=ShopOrder)
    order.id = uuid.uuid4()
    order.order_number = "ORD-260925001"
    order.payment_status = "pending"
    order.payment_through = None
    order.payment_method = None
    order.payment_provider = None
    order.payment_url = None
    order.payment_data = None
    repo_mock.get_by_id.return_value = order

    svc = ShopOrderService(session=session)
    svc._repo = repo_mock
    svc._enqueue_order_created_event = MagicMock()

    pay_data = {"status": "CAPTURED", "ref": "REF123"}
    await svc.update_payment_status(
        order_id=order.id,
        new_payment_status=OrderPaymentStatus.SUCCESS,
        changed_by="staff:123",
        payment_method="knet",
        payment_through="desk",
        payment_provider="MyFatoorah",
        payment_url="https://pay.example.com/receipt/123",
        payment_data=pay_data,
    )

    assert order.payment_status == OrderPaymentStatus.SUCCESS.value
    # "desk" gets normalised to "ushdesk"
    assert order.payment_through == "ushdesk"
    assert order.payment_method == "knet"
    assert order.payment_provider == "MyFatoorah"
    assert order.payment_url == "https://pay.example.com/receipt/123"
    assert order.payment_data == pay_data
    svc._enqueue_order_created_event.assert_called_once_with(order)


def test_shop_order_created_event_fields():
    """Verify ShopOrderCreatedEvent contains payment_through and requester fields."""
    user_id = str(uuid.uuid4())
    event = ShopOrderCreatedEvent(
        order_id=str(uuid.uuid4()),
        order_number="ORD-260925001",
        customer_id=user_id,
        customer_name="Test User",
        payment_through="ushspa",
        payment_type="ushspa",
        order_requested_by_user=user_id,
        order_requested_by_user_data={"id": user_id, "name": "Test User", "image": "avatar.png"},
    )

    assert event.payment_through == "ushspa"
    assert event.payment_type == "ushspa"
    assert event.order_requested_by_user == user_id
    assert event.order_requested_by_user_data["image"] == "avatar.png"


def test_shop_order_created_event_transaction_identifiers():
    """Verify ShopOrderCreatedEvent contains all payment transaction identifiers."""
    event = ShopOrderCreatedEvent(
        order_id=str(uuid.uuid4()),
        order_number="ORD-260925001",
        customer_id=str(uuid.uuid4()),
        reference_id="626810000600",
        track_id="25-09-2026_3843308",
        country="Kuwait",
        payment_id="100626810000007146",
        transaction_id="626810011804085",
        invoice_id="7205938",
        transaction_date="2026-09-25T14:30:43.7433333",
        payment_gateway="KNET",
        transaction_status="Succss",
        created_by="4f5912e2-ebb5-4383-9f42-40df865c4cfb",
        payment_url="https://demo.MyFatoorah.com/checkout",
        payment_data={"invoiceId": "7205938"},
    )

    assert event.reference_id == "626810000600"
    assert event.track_id == "25-09-2026_3843308"
    assert event.country == "Kuwait"
    assert event.payment_id == "100626810000007146"
    assert event.transaction_id == "626810011804085"
    assert event.invoice_id == "7205938"
    assert event.transaction_date == "2026-09-25T14:30:43.7433333"
    assert event.payment_gateway == "KNET"
    assert event.transaction_status == "Succss"
    assert event.created_by == "4f5912e2-ebb5-4383-9f42-40df865c4cfb"
    assert event.payment_url == "https://demo.MyFatoorah.com/checkout"
    assert event.payment_data["invoiceId"] == "7205938"


@pytest.mark.asyncio
async def test_enqueue_order_created_event_extracts_all_transaction_fields():
    """Verify _enqueue_order_created_event extracts transaction fields from payment_data."""
    order = MagicMock(spec=ShopOrder)
    order.id = uuid.uuid4()
    order.order_number = "ORD-260925001"
    order.customer_id = uuid.uuid4()
    order.customer_name = "Mamunur Rashid"
    order.customer_phone = "+96541028983"
    order.contact_number = "+96541028983"
    order.order_requested_by_user = uuid.UUID("4f5912e2-ebb5-4383-9f42-40df865c4cfb")
    order.order_requested_by_user_data = {"id": "4f5912e2-ebb5-4383-9f42-40df865c4cfb"}
    order.formatted_address = "Kuwait City"
    order.public_token = "token_xyz"
    order.tracking_code = "123456"
    order.subtotal = Decimal("27.990")
    order.total_amount = Decimal("27.990")
    order.currency = "KWD"
    order.payment_status = "success"
    order.payment_method = "KNET"
    order.payment_through = "ushspa"
    order.payment_provider = "MyFatoorah"
    order.payment_url = "https://demo.MyFatoorah.com/checkout"
    order.payment_data = {
        "invoiceId": "7205938",
        "data": {
            "InvoiceId": 7205938,
            "InvoiceTransactions": [
                {
                    "TransactionDate": "2026-09-25T14:30:43.7433333",
                    "PaymentGateway": "KNET",
                    "ReferenceId": "626810000600",
                    "TrackId": "25-09-2026_3843308",
                    "TransactionId": "626810011804085",
                    "PaymentId": "100626810000007146",
                    "TransactionStatus": "Succss",
                    "Country": "Kuwait",
                }
            ],
        },
    }
    order.items = []

    svc = ShopOrderService(session=AsyncMock())

    with patch("app.events.sqs_client.get_sqs_client") as mock_sqs_getter:
        mock_sqs = mock_sqs_getter.return_value
        mock_sqs.publish_event = AsyncMock()

        svc._enqueue_order_created_event(order)
        import asyncio
        await asyncio.sleep(0.01)

        mock_sqs.publish_event.assert_called_once()
        published_event = mock_sqs.publish_event.call_args.args[0]

        assert published_event.reference_id == "626810000600"
        assert published_event.track_id == "25-09-2026_3843308"
        assert published_event.country == "Kuwait"
        assert published_event.payment_id == "100626810000007146"
        assert published_event.transaction_id == "626810011804085"
        assert published_event.invoice_id == "7205938"
        assert published_event.transaction_date == "2026-09-25T14:30:43.7433333"
        assert published_event.payment_gateway == "KNET"
        assert published_event.transaction_status == "Succss"
        assert published_event.created_by == "4f5912e2-ebb5-4383-9f42-40df865c4cfb"
        assert published_event.payment_url == "https://demo.MyFatoorah.com/checkout"

