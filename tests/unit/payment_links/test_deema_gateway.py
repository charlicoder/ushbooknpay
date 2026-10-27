"""Unit tests for the Deema (Tap Payments Charges API) payment-link adapter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.payment.domain.gateway_protocol import RefundRequest
from app.payment_links import get_gateway
from app.payment_links.domain.gateway import (
    GatewayPaymentStatus,
    PaymentLinkRequest,
    WebhookEventKind,
)
from app.payment_links.domain.registry import available_gateways, resolve_gateway_name
from app.payment_links.infrastructure.deema_gateway import DeemaGateway

SECRET = "test-tap-secret-key"
MERCHANT_ID = "123456"

CHARGE_CAPTURED_EVENT = {
    "id": "chg_TS02A1720261234567890",
    "object": "charge",
    "status": "CAPTURED",
    "amount": 50,
    "currency": "KWD",
    "reference": {
        "transaction": "txn_001",
        "order": "ord_001",
        "gateway": "gw_12345",
        "payment": "pmt_98765",
        "track": "trk_555",
    },
    "transaction": {
        "created": "1711900000000",
        "url": "https://checkout.tap.company/pay/chg_TS02A1720261234567890",
    },
    "source": {
        "id": "src_deema",
        "payment_method": "DEEMA",
    },
    "metadata": {
        "payment_link_id": "11111111-2222-3333-4444-555555555555",
    },
}


def _settings(secret: str = SECRET) -> SimpleNamespace:
    return SimpleNamespace(
        DEEMA_SECRET_KEY=secret,
        DEEMA_MERCHANT_ID=MERCHANT_ID,
        DEEMA_BASE_URL="https://api.tap.company/v2",
        DEEMA_SOURCE_ID="src_deema",
        DEEMA_CALLBACK_URL="https://example.com/booknpay/api/v1/payment-links/webhook/deema/",
        DEEMA_WEBHOOK_URL="https://example.com/booknpay/api/v1/payment-links/webhook/deema/",
        DEEMA_SUCCESS_URL="http://localhost:3001/payment-links/success",
        DEEMA_ERROR_URL="http://localhost:3001/payment-links/failed",
        DEEMA_DEFAULT_COUNTRY_CODE="+965",
        TAP_SECRET_KEY=secret,
        TAP_MERCHANT_ID=MERCHANT_ID,
        TAP_BASE_URL="https://api.tap.company/v2",
    )


def _gateway(secret: str = SECRET, http_client: object = None) -> DeemaGateway:
    return DeemaGateway(http_client=http_client, settings=_settings(secret))  # type: ignore[arg-type]


def _sign(text: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def test_deema_registered_in_gateway_registry():
    assert "deema" in available_gateways()
    assert "deema_paymentlink" in available_gateways()
    assert resolve_gateway_name("Deema", "myfatoorah") == "deema"
    gw = get_gateway("deema", http_client=None, settings=_settings())  # type: ignore[arg-type]
    assert isinstance(gw, DeemaGateway)
    assert gw.name == "deema"
    assert gw.provider_label == "Deema"


def test_signature_string_matches_tap_docs():
    sig_str = DeemaGateway.build_signature_string(CHARGE_CAPTURED_EVENT)
    assert sig_str == (
        "x_idchg_TS02A1720261234567890"
        "x_amount50.000"
        "x_currencyKWD"
        "x_gateway_referencegw_12345"
        "x_payment_referencepmt_98765"
        "x_statusCAPTURED"
        "x_created1711900000000"
    )


def test_valid_hashstring_accepted_and_tampered_rejected():
    gw = _gateway()
    sig = _sign(DeemaGateway.build_signature_string(CHARGE_CAPTURED_EVENT))
    assert gw.verify_signature({"hashstring": sig}, CHARGE_CAPTURED_EVENT)

    tampered = {**CHARGE_CAPTURED_EVENT, "status": "PENDING"}
    assert not gw.verify_signature({"hashstring": sig}, tampered)


def test_missing_header_or_secret_rejected():
    sig = _sign(DeemaGateway.build_signature_string(CHARGE_CAPTURED_EVENT))
    assert not _gateway().verify_signature({}, CHARGE_CAPTURED_EVENT)
    assert not _gateway(secret="").verify_signature({"hashstring": sig}, CHARGE_CAPTURED_EVENT)


def test_parse_charge_event_captured_and_failed():
    gw = _gateway()
    paid = gw.parse_event(CHARGE_CAPTURED_EVENT)
    assert paid.kind == WebhookEventKind.PAYMENT_STATUS
    assert paid.status == GatewayPaymentStatus.PAID
    assert paid.invoice_id == "chg_TS02A1720261234567890"
    assert paid.state is not None
    assert paid.state.payment_id == "pmt_98765"
    assert paid.state.transaction_id == "txn_001"
    assert paid.state.track_id == "trk_555"
    assert paid.state.reference_id == "11111111-2222-3333-4444-555555555555"
    assert paid.state.payment_method == "deema"
    assert paid.state.amount == Decimal("50")

    failed_payload = {
        "id": "chg_TS02A999",
        "object": "charge",
        "status": "DECLINED",
        "amount": 25,
        "currency": "KWD",
        "response": {"message": "Insufficient balance"},
    }
    failed = gw.parse_event(failed_payload)
    assert failed.status == GatewayPaymentStatus.FAILED
    assert failed.state and failed.state.failure_reason == "Insufficient balance"


def test_parse_refund_webhook_event():
    gw = _gateway()
    refund_payload = {
        "id": "ref_TS01A999",
        "object": "refund",
        "charge_id": "chg_TS02A1720261234567890",
        "status": "REFUNDED",
        "amount": 15.5,
        "currency": "KWD",
    }
    event = gw.parse_event(refund_payload)
    assert event.kind == WebhookEventKind.REFUND_STATUS
    assert event.invoice_id == "chg_TS02A1720261234567890"
    assert event.refund_id == "ref_TS01A999"
    assert event.refund_status == "REFUNDED"
    assert event.refund_amount == Decimal("15.5")


def test_create_link_and_refund_http_payloads():
    mock_http = MagicMock()

    async def _mock_request(method: str, url: str, json=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if method == "POST" and url.endswith("/charges"):
            assert json["source"] == {"id": "src_deema"}
            assert json["merchant"] == {"id": MERCHANT_ID}
            assert json["currency"] == "KWD"
            assert json["customer"]["phone"] == {"country_code": "965", "number": "50001234"}
            resp.json.return_value = {
                "id": "chg_test_123",
                "status": "INITIATED",
                "transaction": {"url": "https://checkout.tap.company/pay/chg_test_123"},
            }
        elif method == "GET" and "/charges/" in url:
            resp.json.return_value = CHARGE_CAPTURED_EVENT
        elif method == "POST" and url.endswith("/refunds"):
            assert json["charge_id"] == "chg_test_123"
            assert json["currency"] == "KWD"
            assert json["reason"] == "requested_by_customer"
            resp.json.return_value = {
                "id": "ref_test_456",
                "status": "PENDING",
            }
        return resp

    mock_http.request = AsyncMock(side_effect=_mock_request)
    gw = _gateway(http_client=mock_http)

    res = asyncio.run(
        gw.create_link(
            PaymentLinkRequest(
                amount=Decimal("45.000"),
                currency="KWD",
                reference="link-uuid-1",
                description="Booking BK-101",
                customer_name="Sara Al-Sabah",
                customer_phone="+96550001234",
                customer_email="sara@example.com",
            )
        )
    )
    assert res.gateway_invoice_id == "chg_test_123"
    assert res.payment_url == "https://checkout.tap.company/pay/chg_test_123"

    state = asyncio.run(gw.get_payment_state("chg_test_123"))
    assert state.status == GatewayPaymentStatus.PAID
    assert state.payment_method == "deema"

    ref = asyncio.run(
        gw.refund_payment(
            RefundRequest(
                provider_payment_id="chg_test_123",
                amount=Decimal("45.000"),
                currency="KWD",
                reason="Booking cancellation",
            )
        )
    )
    assert ref.is_successful is True
    assert ref.provider_refund_id == "ref_test_456"
