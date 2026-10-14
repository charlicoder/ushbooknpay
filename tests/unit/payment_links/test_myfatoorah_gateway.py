"""Unit tests for the MyFatoorah payment-link adapter (signature, parsing, phone split)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from types import SimpleNamespace

from app.payment_links.domain.gateway import GatewayPaymentStatus, WebhookEventKind
from app.payment_links.infrastructure.myfatoorah_gateway import MyFatoorahGateway, split_phone

SECRET = "test-secret"

PAYMENT_EVENT = {
    "Event": {"Code": 1, "Name": "PAYMENT_STATUS_CHANGED", "Reference": "WH-626519"},
    "Data": {
        "Invoice": {"Id": "6409988", "Status": "PAID", "ExternalIdentifier": "asdqwd-f13sdf-fasjkz"},
        "Transaction": {"Id": "86781", "Status": "SUCCESS", "PaymentId": "07076409988323998875"},
        "Amount": {"BaseCurrency": "KWD", "ValueInBaseCurrency": "1"},
    },
}


def _gateway(secret: str = SECRET) -> MyFatoorahGateway:
    settings = SimpleNamespace(MYFATOORAH_WEBHOOK_SECRET=secret, MYFATOORAH_API_KEY="k", MYFATOORAH_BASE_URL="http://x")
    return MyFatoorahGateway(http_client=None, settings=settings)  # type: ignore[arg-type]


def _sign(text: str, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha256).digest()).decode()


def test_signature_string_matches_docs():
    assert MyFatoorahGateway.build_signature_string(PAYMENT_EVENT) == (
        "Invoice.Id=6409988,Invoice.Status=PAID,Transaction.Status=SUCCESS,"
        "Transaction.PaymentId=07076409988323998875,Invoice.ExternalIdentifier=asdqwd-f13sdf-fasjkz"
    )


def test_valid_signature_accepted_and_tampered_rejected():
    gw = _gateway()
    sig = _sign(MyFatoorahGateway.build_signature_string(PAYMENT_EVENT))
    assert gw.verify_signature({"MyFatoorah-Signature": sig}, PAYMENT_EVENT)

    tampered = {**PAYMENT_EVENT, "Data": {**PAYMENT_EVENT["Data"], "Invoice": {**PAYMENT_EVENT["Data"]["Invoice"], "Status": "PENDING"}}}
    assert not gw.verify_signature({"MyFatoorah-Signature": sig}, tampered)


def test_missing_header_or_secret_rejected():
    sig = _sign(MyFatoorahGateway.build_signature_string(PAYMENT_EVENT))
    assert not _gateway().verify_signature({}, PAYMENT_EVENT)
    assert not _gateway(secret="").verify_signature({"myfatoorah-signature": sig}, PAYMENT_EVENT)


def test_refund_event_signature_and_parse():
    payload = {
        "Event": {"Code": 2, "Reference": "WH-1"},
        "Data": {
            "Refund": {"Id": "111147", "Status": "REFUNDED"},
            "Amount": {"ValueInBaseCurrency": "30"},
            "ReferencedInvoice": {"Id": "5620277"},
        },
    }
    assert MyFatoorahGateway.build_signature_string(payload) == (
        "Refund.Id=111147,Refund.Status=REFUNDED,Amount.ValueInBaseCurrency=30,ReferencedInvoice.Id=5620277"
    )
    event = _gateway().parse_event(payload)
    assert event.kind == WebhookEventKind.REFUND_STATUS
    assert event.refund_status == "REFUNDED" and event.invoice_id == "5620277"


def test_parse_payment_event_paid_and_failed():
    gw = _gateway()
    paid = gw.parse_event(PAYMENT_EVENT)
    assert paid.status == GatewayPaymentStatus.PAID and paid.invoice_id == "6409988"
    assert paid.state and paid.state.payment_id == "07076409988323998875"

    failed_payload = {
        "Event": {"Code": 1, "Reference": "WH-2"},
        "Data": {"Invoice": {"Id": "1", "Status": "PENDING"}, "Transaction": {"Status": "FAILED"}},
    }
    assert gw.parse_event(failed_payload).status == GatewayPaymentStatus.FAILED


def test_parse_payment_event_flat_payload():
    gw = _gateway()
    flat_payload = {
        "EventCode": 1,
        "EventName": "TransactionsStatusChanged",
        "Data": {
            "InvoiceId": 7254211,
            "InvoiceStatus": "Paid",
            "TransactionStatus": "SUCCESS",
            "PaymentId": "07077254211387948774",
            "InvoiceValueInBaseCurrency": "429.9",
            "BaseCurrency": "KWD",
        },
    }
    event = gw.parse_event(flat_payload)
    assert event.kind == WebhookEventKind.PAYMENT_STATUS
    assert event.status == GatewayPaymentStatus.PAID
    assert event.invoice_id == "7254211"
    assert event.state and event.state.payment_id == "07077254211387948774"


def test_unknown_event_is_ignored():
    event = _gateway().parse_event({"Event": {"Code": 99, "Reference": "x"}, "Data": {}})
    assert event.kind == WebhookEventKind.IGNORED


def test_split_phone():
    assert split_phone("+96550001234", "+965") == ("+965", "50001234")
    assert split_phone("50001234", "+965") == ("+965", "50001234")
    assert split_phone("96550001234", "+965") == ("+965", "50001234")
    assert split_phone("+971501234567", "+965") == ("+971", "501234567")
    assert split_phone(None, "+965") == ("+965", "")


def test_create_payment_link_request_without_invoice_fields():
    import uuid
    from app.payment_links.api.schemas import CreatePaymentLinkRequest

    # Completely omitted invoice_id and invoice_number
    req = CreatePaymentLinkRequest.model_validate({
        "target_type": "booking",
        "target_id": str(uuid.uuid4()),
        "gateway": "myfatoorah",
    })
    assert req.invoice_id is None
    assert req.invoice_number is None

    # Empty string invoice_id and invoice_number sanitised to None
    req_empty = CreatePaymentLinkRequest.model_validate({
        "target_type": "booking",
        "target_id": str(uuid.uuid4()),
        "invoice_id": "   ",
        "invoice_number": "",
    })
    assert req_empty.invoice_id is None
    assert req_empty.invoice_number is None


def test_transactions_status_changed_with_invoice_transactions():
    gw = _gateway()
    payload = {
        "Event": "TransactionsStatusChanged",
        "Data": {
            "InvoiceId": 9876543,
            "InvoiceStatus": "Paid",
            "InvoiceTransactions": [
                {
                    "PaymentId": "0707987654321",
                    "TransactionStatus": "Succss",
                    "TransactionId": "tx-12345",
                }
            ],
        },
    }
    sig_str = MyFatoorahGateway.build_signature_string(payload)
    assert sig_str is not None
    assert "Invoice.Id=9876543" in sig_str
    assert "Transaction.PaymentId=0707987654321" in sig_str

    event = gw.parse_event(payload)
    assert event.kind == WebhookEventKind.PAYMENT_STATUS
    assert event.status == GatewayPaymentStatus.PAID
    assert event.invoice_id == "9876543"
    assert event.state and event.state.payment_id == "0707987654321"
    assert event.state and event.state.transaction_id == "tx-12345"

