"""
Regression tests for three small bugs fixed before the Course Subscriptions work:

A. ``app.core.security.decode_token`` was imported by ``update_booking_status`` but did not exist.
B. ``PaymentStatusHistory`` was used in ``create_booking`` (loyalty branch) without being imported.
C. ``/payments/initiate/`` referenced the non-existent ``BookingStatus.PENDING``.
"""

from __future__ import annotations

import base64
import inspect
import json
import time

import pytest

from app.core.exceptions import AuthenticationError


def _jwt(claims: dict) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(claims)}.signature"


# ── Bug A ────────────────────────────────────────────────────────────────────


def test_decode_token_returns_payload_from_claims() -> None:
    from app.core.security import decode_token

    token = _jwt(
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "user_type": "employee",
            "phone_number": "+96550000000",
            "first_name": "Sara",
            "last_name": "Ali",
            "exp": time.time() + 600,
        }
    )
    payload = decode_token(token)

    assert payload.sub == "11111111-1111-1111-1111-111111111111"
    assert payload.user_type == "employee"
    assert payload.phone_number == "+96550000000"
    assert payload.first_name == "Sara"
    assert payload.last_name == "Ali"


def test_decode_token_falls_back_to_sub_claim() -> None:
    from app.core.security import decode_token

    payload = decode_token(_jwt({"sub": "abc", "exp": time.time() + 60}))
    assert payload.sub == "abc"
    assert payload.user_type == "customer"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        _jwt({"user_id": "abc", "exp": time.time() - 10}),  # expired
        _jwt({"user_type": "employee"}),  # no subject
    ],
)
def test_decode_token_rejects_invalid_tokens(token: str) -> None:
    from app.core.security import decode_token

    with pytest.raises(AuthenticationError):
        decode_token(token)


# ── Bug B ────────────────────────────────────────────────────────────────────


def test_bookings_module_imports_payment_status_history() -> None:
    from app.api.v1 import bookings
    from app.payment.infrastructure.models import PaymentStatusHistory

    assert bookings.PaymentStatusHistory is PaymentStatusHistory


# ── Bug C ────────────────────────────────────────────────────────────────────


def test_initiate_payment_only_references_existing_booking_statuses() -> None:
    from app.api.v1 import payments
    from app.booking.domain.value_objects import BookingStatus

    source = inspect.getsource(payments.initiate_payment)
    assert "BookingStatus.PENDING." not in source
    assert "BookingStatus.PAYMENT_PENDING" in source
    assert BookingStatus.PAYMENT_PENDING.value == "payment_pending"

