"""
tests/unit/payment/test_payments_crud_and_parser.py
────────────────────────────────────────────────────
Unit tests for payment gateway response parser and schemas.
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
    assert parsed["provider"] == "myfatoorah"

    # Identifiers
    assert parsed["provider_payment_id"] == "7103271"
    assert parsed["invoice_reference"] == "2026193793"
    assert parsed["customer_reference"] == "ORDER_1787619564599"
    assert parsed["reference_id"] == "623710000033"
    assert parsed["track_id"] == "25-08-2026_3759584"
    assert parsed["provider_transaction_id"] == "623710001297726"
    assert parsed["authorization_id"] == "B61005"
    assert parsed["payment_id_gateway"] == "100623710000000606"
    assert parsed["gateway_name"] == "KNET"
    assert parsed["payment_method"] == "knet"

    # Financial Breakdown
    assert parsed["amount"] == Decimal("25.000")
    assert parsed["currency"] == "KWD"
    assert parsed["service_charge"] == Decimal("1.250")
    assert parsed["vat_amount"] == Decimal("0.188")
    assert parsed["due_deposit"] == Decimal("23.562")
    assert parsed["deposit_status"] == "Not Deposited"

    # Customer & Network
    assert parsed["customer_data"]["name"] == "USH SPA Customer"
    assert parsed["customer_data"]["mobile"] == "+96541028982"
    assert parsed["customer_data"]["email"] == "none@noemail.com"
    assert parsed["ip_address"] == "83.96.112.1"
    assert parsed["payment_url"] == payload["paymentUrl"]

    # 15 Unified Standard Fields
    assert parsed["payment_id"] == "100623710000000606"
    assert parsed["transaction_id"] == "623710001297726"
    assert parsed["is_paid"] is True
    assert parsed["invoice_id"] == "7103271"
    assert parsed["status"] == "success"
    assert parsed["invoice_reference"] == "2026193793"
    assert parsed["customer_reference"] == "ORDER_1787619564599"
    assert parsed["created_date"] == "2026-08-25T03:59:25.017"
    assert parsed["invoice_value"] == Decimal("25.000")
    assert parsed["customer_name"] == "USH SPA Customer"
    assert parsed["customer_mobile"] == "+96541028982"
    assert parsed["customer_email"] == "none@noemail.com"
    assert parsed["transaction_date"] == "2026-08-25T03:59:57.0833333"
    assert parsed["payment_gateway"] == "KNET"


def test_create_and_detail_payment_schema_with_unified_fields():
    """Test CreatePaymentRequestSchema and PaymentDetailResponse with all 15 unified fields."""
    import uuid
    from app.payment.interfaces.schemas import CreatePaymentRequestSchema, PaymentDetailResponse

    booking_id = str(uuid.uuid4())
    customer_id = str(uuid.uuid4())

    data = {
        "payment_id": "100623710000000606",
        "transaction_id": "623710001297726",
        "booking_id": booking_id,
        "customer_id": customer_id,
        "is_paid": True,
        "invoice_id": "7103271",
        "status": "success",
        "invoice_reference": "2026193793",
        "customer_reference": "ORDER_1787619564599",
        "created_date": "2026-08-25T03:59:25.017",
        "invoice_value": "25.000",
        "customer_name": "USH SPA Customer",
        "customer_mobile": "+96541028982",
        "customer_email": "none@noemail.com",
        "transaction_date": "2026-08-25T03:59:57.0833333",
        "payment_gateway": "KNET",
        "amount": "25.000",
        "currency": "KWD",
        "provider": "myfatoorah",
        "payment_method": "knet",
    }

    req = CreatePaymentRequestSchema(**data)
    assert req.payment_id == "100623710000000606"
    assert req.transaction_id == "623710001297726"
    assert req.is_paid is True
    assert req.invoice_id == "7103271"
    assert Decimal(str(req.invoice_value)) == Decimal("25.000")
    assert req.customer_name == "USH SPA Customer"
    assert req.customer_mobile == "+96541028982"
    assert req.customer_email == "none@noemail.com"
    assert req.created_date == "2026-08-25T03:59:25.017"
    assert req.transaction_date == "2026-08-25T03:59:57.0833333"
    assert req.payment_gateway == "KNET"


def test_payment_for_enum_values():
    """Verify all allowed values for PaymentFor enum."""
    from app.payment.domain.value_objects import PaymentFor

    assert PaymentFor.GIFT_VOUCHER.value == "gift_voucher"
    assert PaymentFor.SERVICE.value == "service"
    assert PaymentFor.HOME_SERVICE.value == "home_service"
    assert PaymentFor.PRODUCTS.value == "products"
    assert PaymentFor.LOYALTY.value == "loyalty"
    assert PaymentFor.OTHERS.value == "others"


def test_payment_model_optional_booking_and_voucher_fields():
    """Verify Payment ORM model can be instantiated with booking_id=None and voucher fields."""
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
        amount=Decimal("50.000"),
        currency="KWD",
        provider="myfatoorah",
        payment_method="knet",
        status="success",
    )
    assert p.booking_id is None
    assert p.voucher_id == voucher_id
    assert p.voucher_data == voucher_data
    assert p.payment_for == "gift_voucher"
    assert "for=gift_voucher" in repr(p)

    # Case 2: Payment default payment_for
    p2 = Payment(
        customer_id=customer_id,
        amount=Decimal("20.000"),
    )
    assert p2.payment_for == PaymentFor.SERVICE.value
    assert p2.booking_id is None
    assert p2.voucher_id is None


def test_schemas_with_voucher_and_optional_booking():
    """Verify request and response schemas support voucher fields and optional booking_id."""
    import uuid
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
        voucher_data=vouch_data,
        payment_for=PaymentFor.GIFT_VOUCHER,
    )
    assert init_req.booking_id is None
    assert init_req.voucher_id == vouch_id
    assert init_req.payment_for == PaymentFor.GIFT_VOUCHER

    # 2. CreatePaymentRequestSchema
    create_req = CreatePaymentRequestSchema(
        booking_id=None,
        customer_id=cust_id,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
        amount="50.000",
    )
    assert create_req.booking_id is None
    assert create_req.voucher_id == vouch_id
    assert create_req.payment_for == "gift_voucher"

    # 3. UpdatePaymentRequestSchema
    update_req = UpdatePaymentRequestSchema(
        booking_id=None,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
    )
    assert update_req.voucher_id == vouch_id
    assert update_req.payment_for == "gift_voucher"

    # 4. PaymentDetailResponse & PaymentListItem
    from datetime import datetime
    now = datetime.now()

    detail = PaymentDetailResponse(
        id=str(uuid.uuid4()),
        booking_id=None,
        customer_id=cust_id,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
        amount="50.000",
        currency="KWD",
        provider="myfatoorah",
        payment_method="knet",
        status="success",
        created_at=now,
        updated_at=now,
    )
    assert detail.booking_id is None
    assert detail.voucher_id == vouch_id
    assert detail.payment_for == "gift_voucher"

    list_item = PaymentListItem(
        id=str(uuid.uuid4()),
        booking_id=None,
        customer_id=cust_id,
        voucher_id=vouch_id,
        voucher_data=vouch_data,
        payment_for="gift_voucher",
        amount="50.000",
        currency="KWD",
        provider="myfatoorah",
        payment_method="knet",
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
        provider="myfatoorah",
        payment_url="https://pay.example.com",
        amount="50.000",
        currency="KWD",
    )
    assert session_resp.booking_id is None
    assert session_resp.voucher_id == vouch_id

    status_resp = PaymentStatusResponse(
        payment_id="pay-123",
        booking_id=None,
        voucher_id=vouch_id,
        payment_for="gift_voucher",
        provider="myfatoorah",
        status="success",
        amount="50.000",
        currency="KWD",
        payment_method="knet",
        provider_reference="INV-123",
        created_at=now,
        updated_at=now,
    )
    assert status_resp.booking_id is None
    assert status_resp.voucher_id == vouch_id


def test_payment_to_detail_and_list_item_mappers():
    """Verify _payment_to_detail and _payment_to_list_item safely handle None booking_id and map voucher fields."""
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
        amount=Decimal("75.000"),
        currency="KWD",
        provider="tap",
        payment_method="apple_pay",
        status="success",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    detail = _payment_to_detail(p)
    assert detail.booking_id is None
    assert detail.voucher_id == str(vouch_id)
    assert detail.voucher_data == vouch_data
    assert detail.payment_for == "gift_voucher"

    list_item = _payment_to_list_item(p)
    assert list_item.booking_id is None
    assert list_item.voucher_id == str(vouch_id)
    assert list_item.voucher_data == vouch_data
    assert list_item.payment_for == "gift_voucher"


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
