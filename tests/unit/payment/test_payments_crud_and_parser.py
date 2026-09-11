"""
tests/unit/payment/test_payments_crud_and_parser.py
────────────────────────────────────────────────────
Unit tests for payment gateway response parser, schemas, and model.
Updated to reflect the 2026-09-10 payments model overhaul:
- amount → total_amount (required)
- total_duration (required)
- payment_provider replaces provider
- payment_gateway replaces gateway_name
- PaymentFor: branch_service, home_service, gift_voucher, product_items
- payment_data JSONB consolidates all gateway-specific identifiers
"""

from decimal import Decimal
from app.payment.domain.parser import parse_gateway_response
from app.payment.domain.value_objects import PaymentTransactionStatus


def test_parse_myfatoorah_full_gateway_response():
    """Test parsing the exact MyFatoorah gateway response data from the user prompt."""
    payload = {
        "isPaid": True,
        "invoiceId": "7103271",
        "status": "Paid",
        "paymentUrl": "https://demo.MyFatoorah.com/En/KWT/PayInvoice/Checkout?invoiceKey=01072710327140-986958dd&paymentGatewayId=1121",
        "data": {
            "InvoiceId": 7103271,
            "InvoiceStatus": "Paid",
            "InvoiceReference": "2026193793",
            "CustomerReference": "ORDER_1787619564599",
            "CreatedDate": "2026-08-25T03:59:25.017",
            "ExpiryDate": "August 28, 2026",
            "ExpiryTime": "03:59:25.017",
            "InvoiceValue": 25,
            "Comments": None,
            "CustomerName": "USH SPA Customer",
            "CustomerMobile": "+96541028982",
            "CustomerEmail": "none@noemail.com",
            "UserDefinedField": None,
            "InvoiceDisplayValue": "25.000 KD",
            "DueDeposit": 23.562,
            "DepositStatus": "Not Deposited",
            "InvoiceItems": [],
            "InvoiceTransactions": [
                {
                    "TransactionDate": "2026-08-25T03:59:57.0833333",
                    "PaymentGateway": "KNET",
                    "ReferenceId": "623710000033",
                    "TrackId": "25-08-2026_3759584",
                    "TransactionId": "623710001297726",
                    "PaymentId": "100623710000000606",
                    "AuthorizationId": "B61005",
                    "TransactionStatus": "Succss",
                    "TransationValue": "25.000",
                    "CustomerServiceCharge": "0.000",
                    "TotalServiceCharge": "1.250",
                    "DueValue": "25.000",
                    "PaidCurrency": "KD",
                    "PaidCurrencyValue": "25.000",
                    "VatAmount": "0.188",
                    "IpAddress": "83.96.112.1",
                    "Country": "Kuwait",
                    "Currency": "KD",
                    "Error": None,
                    "CardNumber": None,
                    "ErrorCode": "",
                    "ECI": None,
                    "Card": {
                        "NameOnCard": "",
                        "Number": "",
                        "Token": "",
                        "PanHash": "",
                        "ExpiryMonth": "",
                        "ExpiryYear": "",
                        "Brand": "",
                        "Issuer": "",
                        "IssuerCountry": "",
                        "FundingMethod": "",
                    },
                }
            ],
            "Suppliers": [],
        },
    }

    parsed = parse_gateway_response(payload)

    # Status
    assert parsed["status"] == PaymentTransactionStatus.SUCCESS.value

    # Top-level mapped fields (new schema)
    assert parsed["total_amount"] == Decimal("25.000")
    assert parsed["currency"] == "KWD"
    assert parsed["invoice_id"] == "7103271"
    assert parsed["transaction_id"] == "623710001297726"
    assert parsed["payment_id"] == "100623710000000606"
    assert parsed["reference_id"] == "623710000033"
    assert parsed["track_id"] == "25-08-2026_3759584"
    assert parsed["transaction_status"] == "Succss"
    assert parsed["transaction_date"] == "2026-08-25T03:59:57.0833333"
    assert parsed["payment_method"] == "knet"
    assert parsed["payment_gateway"] == "KNET"
    assert parsed["payment_url"] == payload["paymentUrl"]
    assert parsed["country"] == "Kuwait"

    # Customer data snapshot
    assert parsed["customer_data"]["name"] == "USH SPA Customer"
    assert parsed["customer_data"]["mobile"] == "+96541028982"
    assert parsed["customer_data"]["email"] == "none@noemail.com"

    # payment_data blob consolidates gateway-specific identifiers
    pdata = parsed["payment_data"]
    assert pdata["provider_payment_id"] == "7103271"
    assert pdata["invoice_reference"] == "2026193793"
    assert pdata["customer_reference"] == "ORDER_1787619564599"
    assert pdata["authorization_id"] == "B61005"
    assert pdata["ip_address"] == "83.96.112.1"
    assert pdata["raw_response"] is payload


def test_create_and_detail_payment_schema_with_unified_fields():
    """Test CreatePaymentRequestSchema with new required and optional fields."""
    import uuid
    from app.payment.interfaces.schemas import CreatePaymentRequestSchema, PaymentDetailResponse

    customer_id = str(uuid.uuid4())

    data = {
        # Required fields
        "customer_id": customer_id,
        "total_amount": "25.000",
        "total_duration": 60,
        "currency": "KWD",
        # Optional payment classification
        "payment_id": "100623710000000606",
        "transaction_id": "623710001297726",
        "invoice_id": "7103271",
        "status": "success",
        "transaction_date": "2026-08-25T03:59:57.0833333",
        "payment_gateway": "KNET",
        "payment_provider": "MyFatoorah",
        "payment_method": "knet",
        "reference_id": "623710000033",
        "track_id": "25-08-2026_3759584",
        "invoice_value": "25.000",
    }

    req = CreatePaymentRequestSchema(**data)
    assert req.payment_id == "100623710000000606"
    assert req.transaction_id == "623710001297726"
    assert req.invoice_id == "7103271"
    assert Decimal(str(req.invoice_value)) == Decimal("25.000")
    assert Decimal(str(req.total_amount)) == Decimal("25.000")
    assert req.total_duration == 60
    assert req.transaction_date == "2026-08-25T03:59:57.0833333"
    assert req.payment_gateway == "KNET"
    assert req.payment_method == "knet"


def test_payment_for_enum_values():
    """Verify all allowed values for the new PaymentFor enum."""
    from app.payment.domain.value_objects import PaymentFor

    assert PaymentFor.BRANCH_SERVICE.value == "branch_service"
    assert PaymentFor.HOME_SERVICE.value == "home_service"
    assert PaymentFor.GIFT_VOUCHER.value == "gift_voucher"
    assert PaymentFor.PRODUCT_ITEMS.value == "product_items"

    # Test normalise helper
    assert PaymentFor.normalise("service") == PaymentFor.BRANCH_SERVICE
    assert PaymentFor.normalise("branch_service") == PaymentFor.BRANCH_SERVICE
    assert PaymentFor.normalise("home_service") == PaymentFor.HOME_SERVICE
    assert PaymentFor.normalise("gift_voucher") == PaymentFor.GIFT_VOUCHER
    assert PaymentFor.normalise("products") == PaymentFor.PRODUCT_ITEMS
    assert PaymentFor.normalise("voucher") == PaymentFor.GIFT_VOUCHER


def test_payment_provider_enum_values():
    """Verify PaymentProvider enum values and normalise helper."""
    from app.payment.domain.value_objects import PaymentProvider

    assert PaymentProvider.MYFATOORAH.value == "MyFatoorah"
    assert PaymentProvider.DIRECTLINK.value == "DirectLink"
    assert PaymentProvider.DEEMA.value == "Deema"
    assert PaymentProvider.OTHER.value == "Other"

    # Normalise
    assert PaymentProvider.normalise("myfatoorah") == PaymentProvider.MYFATOORAH
    assert PaymentProvider.normalise("myfatora") == PaymentProvider.MYFATOORAH
    assert PaymentProvider.normalise("directlink") == PaymentProvider.DIRECTLINK
    assert PaymentProvider.normalise("deema") == PaymentProvider.DEEMA
    assert PaymentProvider.normalise("tap") == PaymentProvider.OTHER


def test_payment_through_enum_values():
    """Verify PaymentThrough enum values and normalise helper."""
    from app.payment.domain.value_objects import PaymentThrough

    assert PaymentThrough.USHSPA.value == "ushspa"
    assert PaymentThrough.DESK.value == "desk"
    assert PaymentThrough.OTHER.value == "other"

    assert PaymentThrough.normalise("ushspa") == PaymentThrough.USHSPA
    assert PaymentThrough.normalise("app") == PaymentThrough.USHSPA
    assert PaymentThrough.normalise("desk") == PaymentThrough.DESK
    assert PaymentThrough.normalise("pos") == PaymentThrough.DESK


def test_payment_gateway_enum_values():
    """Verify PaymentGateway enum values and normalise helper."""
    from app.payment.domain.value_objects import PaymentGateway

    assert PaymentGateway.KNET.value == "KNET"
    assert PaymentGateway.TAP.value == "TAP"
    assert PaymentGateway.OTHER.value == "Other"

    assert PaymentGateway.normalise("KNET") == PaymentGateway.KNET
    assert PaymentGateway.normalise("knet") == PaymentGateway.KNET
    assert PaymentGateway.normalise("TAP") == PaymentGateway.TAP
    assert PaymentGateway.normalise("visa") == PaymentGateway.OTHER


def test_payment_model_optional_booking_and_voucher_fields():
    """Verify Payment ORM model can be instantiated with new required fields."""
    import uuid
    from app.payment.infrastructure.models import Payment
    from app.payment.domain.value_objects import PaymentFor

    customer_id = uuid.uuid4()
    voucher_id = uuid.uuid4()
    voucher_data = {"code": "GIFT50", "value": 50.0, "recipient_name": "Fatima"}

    # Case 1: Payment for a gift voucher without booking_id
    p = Payment(
        booking_id=None,
        customer_id=customer_id,
        voucher_id=voucher_id,
        voucher_data=voucher_data,
        payment_for=PaymentFor.GIFT_VOUCHER.value,
        total_amount=Decimal("50.000"),
        total_duration=60,
        currency="KWD",
        payment_provider="MyFatoorah",
        payment_method="knet",
        status="success",
    )
    assert p.booking_id is None
    assert p.voucher_id == voucher_id
    assert p.voucher_data == voucher_data
    assert p.payment_for == "gift_voucher"
    assert p.total_amount == Decimal("50.000")
    assert p.total_duration == 60

    # Case 2: Minimal payment — SQLAlchemy column defaults fire at INSERT not __init__,
    # so payment_for may be None at construction or the Python default value
    p2 = Payment(
        customer_id=customer_id,
        total_amount=Decimal("20.000"),
        total_duration=30,
        currency="KWD",
    )
    assert p2.payment_for in (PaymentFor.BRANCH_SERVICE.value, None)
    assert p2.booking_id is None
    assert p2.voucher_id is None



def test_schemas_with_voucher_and_optional_booking():
    """Verify request and response schemas support voucher fields and optional booking_id."""
    import uuid
    from datetime import datetime
    from app.payment.domain.value_objects import PaymentFor
    from app.payment.interfaces.schemas import (
        InitiatePaymentRequest,
        CreatePaymentRequestSchema,
        UpdatePaymentRequestSchema,
        PaymentListItem,
        PaymentDetailResponse,
        PaymentSessionResponse,
        PaymentStatusResponse,
    )

    cust_id = str(uuid.uuid4())
    vouch_id = str(uuid.uuid4())
    vouch_data = {"code": "PROMO2026", "discount": "20%"}

    # 1. InitiatePaymentRequest without booking_id
    init_req = InitiatePaymentRequest(
        booking_id=None,
        voucher_id=vouch_id,
        payment_for=PaymentFor.GIFT_VOUCHER.value,
    )
    assert init_req.booking_id is None
    assert init_req.voucher_id == vouch_id
    assert init_req.payment_for == "gift_voucher"

    # 2. CreatePaymentRequestSchema — now requires total_amount, total_duration, currency
    create_req = CreatePaymentRequestSchema(
        customer_id=cust_id,
        total_amount="50.000",
        total_duration=60,
        currency="KWD",
        booking_id=None,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
    )
    assert create_req.booking_id is None
    assert str(create_req.voucher_id) == vouch_id
    assert create_req.payment_for == "gift_voucher"
    assert Decimal(str(create_req.total_amount)) == Decimal("50.000")
    assert create_req.total_duration == 60

    # 3. UpdatePaymentRequestSchema — all optional
    update_req = UpdatePaymentRequestSchema(
        booking_id=None,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
    )
    assert str(update_req.voucher_id) == vouch_id
    assert update_req.payment_for == "gift_voucher"

    # 4. PaymentDetailResponse & PaymentListItem
    now = datetime.now()

    detail = PaymentDetailResponse(
        id=str(uuid.uuid4()),
        booking_id=None,
        customer_id=cust_id,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
        total_amount="50.000",
        total_duration=60,
        currency="KWD",
        status="success",
        created_at=now,
    )
    assert detail.booking_id is None
    assert detail.voucher_id == vouch_id
    assert detail.payment_for == "gift_voucher"

    list_item = PaymentListItem(
        id=str(uuid.uuid4()),
        booking_id=None,
        customer_id=cust_id,
        voucher_id=vouch_id,
        payment_for="gift_voucher",
        total_amount="50.000",
        total_duration=60,
        currency="KWD",
        status="success",
        created_at=now,
    )
    assert list_item.booking_id is None
    assert list_item.voucher_id == vouch_id
    assert list_item.payment_for == "gift_voucher"

    # 5. PaymentSessionResponse & PaymentStatusResponse
    session_resp = PaymentSessionResponse(
        payment_id="pay-123",
        booking_id=None,
        voucher_id=vouch_id,
        payment_for="gift_voucher",
        provider="MyFatoorah",
        payment_url="https://pay.example.com",
        total_amount="50.000",
        currency="KWD",
    )
    assert session_resp.booking_id is None
    assert session_resp.voucher_id == vouch_id

    status_resp = PaymentStatusResponse(
        payment_id="pay-123",
        booking_id=None,
        voucher_id=vouch_id,
        payment_for="gift_voucher",
        payment_provider="MyFatoorah",
        status="success",
        total_amount="50.000",
        currency="KWD",
        payment_method="knet",
        reference_id="INV-123",
        created_at=now,
    )
    assert status_resp.booking_id is None
    assert status_resp.voucher_id == vouch_id


def test_payment_to_detail_and_list_item_mappers():
    """Verify _payment_to_detail and _payment_to_list_item map new model fields correctly."""
    import uuid
    from datetime import datetime
    from app.api.v1.payments import _payment_to_detail, _payment_to_list_item
    from app.payment.infrastructure.models import Payment

    cust_id = uuid.uuid4()
    vouch_id = uuid.uuid4()
    vouch_data = {"title": "Spa Voucher", "value": 75}

    p = Payment(
        id=uuid.uuid4(),
        booking_id=None,
        customer_id=cust_id,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
        total_amount=Decimal("75.000"),
        total_duration=90,
        currency="KWD",
        payment_provider="DirectLink",
        payment_method="apple_pay",
        status="success",
        created_at=datetime.now(),
    )

    detail = _payment_to_detail(p)
    assert detail.booking_id is None
    assert detail.voucher_id == str(vouch_id)
    assert detail.voucher_data == vouch_data
    assert detail.payment_for == "gift_voucher"
    assert detail.total_amount == "75.000"
    assert detail.total_duration == 90
    assert detail.payment_provider == "DirectLink"

    list_item = _payment_to_list_item(p)
    assert list_item.booking_id is None
    assert list_item.voucher_id == str(vouch_id)
    assert list_item.payment_for == "gift_voucher"
    assert list_item.total_amount == "75.000"
    assert list_item.total_duration == 90


def test_event_contracts_with_voucher_and_optional_booking():
    """Verify event contracts serialize properly with optional booking_id and voucher_id."""
    from app.events.contracts import (
        PaymentInitiatedEvent,
        PaymentSucceededEvent,
        PaymentFailedEvent,
        RefundIssuedEvent,
    )

    ev_init = PaymentInitiatedEvent(
        payment_id="p-1",
        booking_id=None,
        voucher_id="v-1",
        payment_for="gift_voucher",
        customer_id="c-1",
        amount="30.000",
    )
    assert ev_init.booking_id is None
    assert ev_init.voucher_id == "v-1"
    assert ev_init.payment_for == "gift_voucher"

    ev_succ = PaymentSucceededEvent(
        payment_id="p-1",
        booking_id=None,
        voucher_id="v-1",
        payment_for="gift_voucher",
    )
    assert ev_succ.booking_id is None
    assert ev_succ.voucher_id == "v-1"
    assert ev_succ.payment_for == "gift_voucher"
