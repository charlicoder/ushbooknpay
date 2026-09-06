"""
app/payment/domain/parser.py
────────────────────────────
Unified parser for payment gateway responses (e.g. MyFatoorah, Tap).

Extracts and normalizes all financial data, gateway references, customer snapshots,
transaction attributes, charges, and deposit information for auditing and dashboards.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.payment.domain.value_objects import PaymentTransactionStatus


def _safe_decimal(value: Any, default: Decimal = Decimal("0.000")) -> Decimal:
    """Safely convert a string, int, or float to Decimal with fallback."""
    if value is None or value == "":
        return default
    try:
        # Strip currency symbols if present e.g. "25.000 KD" -> "25.000"
        clean = str(value).replace("KD", "").replace("KWD", "").replace(",", "").strip()
        return Decimal(clean)
    except Exception:
        return default


def _safe_datetime(value: Any) -> datetime | None:
    """Safely parse ISO datetime string."""
    if not value or not isinstance(value, str):
        return None
    try:
        # Handle fractional seconds and spaces
        cleaned = value.strip().replace(" ", "T")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


def parse_gateway_response(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Parse full gateway response dictionary into a standardized payment dictionary.

    Supports:
    - MyFatoorah webhook & direct payment page response
    - Tap charge response
    - Generic gateway payloads
    """
    if not isinstance(payload, dict):
        return {}

    # Check for wrapped 'data' or 'Data'
    data = payload.get("data") or payload.get("Data") or {}
    if not isinstance(data, dict):
        data = {}

    # Extract transactions if present (MyFatoorah InvoiceTransactions)
    transactions = data.get("InvoiceTransactions") or data.get("invoice_transactions") or []
    first_txn: dict[str, Any] = transactions[0] if isinstance(transactions, list) and len(transactions) > 0 and isinstance(transactions[0], dict) else {}

    # ── 1. Status Determination ───────────────────────────────────────────
    is_paid_flag = payload.get("isPaid") is True or payload.get("IsSuccess") is True or payload.get("is_paid") is True
    invoice_status = str(data.get("InvoiceStatus") or payload.get("status") or "").lower()
    txn_status = str(first_txn.get("TransactionStatus") or "").lower()

    if is_paid_flag or invoice_status == "paid" or "succ" in txn_status or invoice_status == "success" or invoice_status == "captured":
        status = PaymentTransactionStatus.SUCCESS.value
    elif invoice_status in ("failed", "error") or "fail" in txn_status:
        status = PaymentTransactionStatus.FAILED.value
    elif invoice_status in ("cancelled", "canceled"):
        status = PaymentTransactionStatus.CANCELLED.value
    elif invoice_status in ("pending", "initiated"):
        status = PaymentTransactionStatus.PENDING.value
    else:
        status = PaymentTransactionStatus.SUCCESS.value if is_paid_flag else PaymentTransactionStatus.PENDING.value

    # ── 2. Financial Breakdown ────────────────────────────────────────────
    raw_amount = (
        data.get("InvoiceValue")
        or first_txn.get("TransationValue")
        or first_txn.get("DueValue")
        or payload.get("amount")
        or data.get("InvoiceDisplayValue")
    )
    amount = _safe_decimal(raw_amount, Decimal("0.000"))

    currency = str(
        first_txn.get("Currency")
        or first_txn.get("PaidCurrency")
        or data.get("CurrencyIso")
        or payload.get("currency")
        or "KWD"
    ).upper()
    if currency in ("KD", "KWD"):
        currency = "KWD"

    service_charge = _safe_decimal(first_txn.get("TotalServiceCharge") or data.get("TotalServiceCharge"))
    vat_amount = _safe_decimal(first_txn.get("VatAmount") or data.get("VatAmount"))
    due_deposit_val = data.get("DueDeposit")
    due_deposit = _safe_decimal(due_deposit_val) if due_deposit_val is not None else None
    deposit_status = str(data.get("DepositStatus") or "Not Deposited")

    # ── 3. Gateway & Transaction Identifiers ──────────────────────────────
    invoice_id = str(
        data.get("InvoiceId")
        or payload.get("invoiceId")
        or payload.get("InvoiceId")
        or ""
    ).strip()
    invoice_reference = str(data.get("InvoiceReference") or payload.get("invoice_reference") or "").strip()
    customer_reference = str(data.get("CustomerReference") or payload.get("customer_reference") or "").strip()

    gateway_name = str(first_txn.get("PaymentGateway") or payload.get("gateway") or "").strip() or None
    reference_id = str(first_txn.get("ReferenceId") or "").strip() or None
    track_id = str(first_txn.get("TrackId") or "").strip() or None
    authorization_id = str(first_txn.get("AuthorizationId") or "").strip() or None
    transaction_id = str(first_txn.get("TransactionId") or payload.get("transaction_id") or "").strip() or None
    payment_id_gateway = str(first_txn.get("PaymentId") or "").strip() or None

    # Determine payment method
    payment_method = "knet" if gateway_name and "knet" in gateway_name.lower() else "card"
    if gateway_name:
        if "apple" in gateway_name.lower():
            payment_method = "apple_pay"
        elif "google" in gateway_name.lower():
            payment_method = "google_pay"

    # ── 4. Customer Details ───────────────────────────────────────────────
    customer_name = str(data.get("CustomerName") or payload.get("customer_name") or "").strip()
    customer_mobile = str(data.get("CustomerMobile") or payload.get("customer_mobile") or "").strip()
    customer_email = str(data.get("CustomerEmail") or payload.get("customer_email") or "").strip()

    customer_data = {
        "name": customer_name,
        "mobile": customer_mobile,
        "phone_number": customer_mobile,
        "email": customer_email,
    }

    # ── 5. Network / Device / Geo ─────────────────────────────────────────
    ip_address = str(first_txn.get("IpAddress") or payload.get("ip_address") or "").strip() or None
    country = str(first_txn.get("Country") or "").strip() or None

    # ── 6. Card Information (Safe only - no full PAN/CVV) ──────────────────
    card_raw = first_txn.get("Card") or {}
    card_info = None
    if isinstance(card_raw, dict) and card_raw:
        card_info = {
            "name_on_card": card_raw.get("NameOnCard", ""),
            "brand": card_raw.get("Brand", ""),
            "issuer": card_raw.get("Issuer", ""),
            "issuer_country": card_raw.get("IssuerCountry", ""),
            "funding_method": card_raw.get("FundingMethod", ""),
            "pan_hash": card_raw.get("PanHash", ""),
            "expiry_month": card_raw.get("ExpiryMonth", ""),
            "expiry_year": card_raw.get("ExpiryYear", ""),
        }

    # ── 7. URLs & Timestamps ──────────────────────────────────────────────
    payment_url = str(payload.get("paymentUrl") or data.get("PaymentURL") or "").strip() or None
    created_date_raw = str(data.get("CreatedDate") or payload.get("created_date") or payload.get("CreatedDate") or "").strip() or None
    transaction_date_raw = str(first_txn.get("TransactionDate") or payload.get("transaction_date") or payload.get("TransactionDate") or "").strip() or None
    paid_at_raw = transaction_date_raw or created_date_raw or payload.get("paid_at")
    paid_at = _safe_datetime(paid_at_raw)

    final_payment_id = payment_id_gateway or str(data.get("PaymentId") or payload.get("payment_id") or payload.get("PaymentId") or "").strip() or None
    final_transaction_id = transaction_id or str(payload.get("transaction_id") or payload.get("TransactionId") or "").strip() or None
    final_payment_gateway = gateway_name or str(payload.get("payment_gateway") or payload.get("PaymentGateway") or "").strip() or None
    is_paid = is_paid_flag or (status == PaymentTransactionStatus.SUCCESS.value)

    return {
        "provider": "myfatoorah",
        "provider_payment_id": invoice_id or None,
        "provider_reference": invoice_reference or invoice_id or None,
        "provider_transaction_id": final_transaction_id,
        "payment_id": final_payment_id,
        "transaction_id": final_transaction_id,
        "is_paid": is_paid,
        "invoice_id": invoice_id or None,
        "invoice_value": amount,
        "customer_name": customer_name or None,
        "customer_mobile": customer_mobile or None,
        "customer_email": customer_email or None,
        "created_date": created_date_raw,
        "transaction_date": transaction_date_raw,
        "payment_gateway": final_payment_gateway,
        "invoice_reference": invoice_reference or None,
        "customer_reference": customer_reference or None,
        "gateway_name": final_payment_gateway or gateway_name,
        "payment_method": payment_method,
        "reference_id": reference_id,
        "track_id": track_id,
        "authorization_id": authorization_id,
        "payment_id_gateway": payment_id_gateway,
        "amount": amount,
        "currency": currency,
        "service_charge": service_charge,
        "vat_amount": vat_amount,
        "due_deposit": due_deposit,
        "deposit_status": deposit_status,
        "status": status,
        "customer_data": customer_data,
        "ip_address": ip_address,
        "country": country,
        "card_info": card_info,
        "payment_url": payment_url,
        "paid_at": paid_at,
        "provider_response": payload,
    }
