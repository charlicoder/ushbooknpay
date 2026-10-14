"""Unit tests for PaymentLinkService booking loading and cancelled status support."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.booking.infrastructure.models import Booking
from app.booking.domain.value_objects import BookingStatus
from app.core.exceptions import ValidationError
from app.payment_links.application.link_service import (
    _BOOKING_LINKABLE,
    PaymentLinkService,
)


def test_booking_linkable_includes_cancelled():
    assert BookingStatus.CANCELLED.value in _BOOKING_LINKABLE
    assert "cancelled" in _BOOKING_LINKABLE
    assert BookingStatus.CONFIRMED.value in _BOOKING_LINKABLE
    assert BookingStatus.PAYMENT_PENDING.value in _BOOKING_LINKABLE


@pytest.mark.asyncio
async def test_load_booking_allows_cancelled_paid_booking():
    session = MagicMock()
    settings = MagicMock()
    gateway = MagicMock()
    gateway.name = "myfatoorah"

    service = PaymentLinkService(session, settings, gateway)

    booking = MagicMock(spec=Booking)
    booking.id = uuid.uuid4()
    booking.status = "cancelled"
    booking.payment_status = "refunded"
    booking.total_amount = Decimal("28.000")

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = booking
    session.execute = AsyncMock(return_value=result_mock)

    loaded = await service._load_booking(booking.id)
    assert loaded == booking


@pytest.mark.asyncio
async def test_load_booking_rejects_unsupported_status():
    session = MagicMock()
    settings = MagicMock()
    gateway = MagicMock()

    service = PaymentLinkService(session, settings, gateway)

    booking = MagicMock(spec=Booking)
    booking.id = uuid.uuid4()
    booking.status = "no_show"
    booking.payment_status = "pending"

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = booking
    session.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(ValidationError, match="A payment link can only be created for confirmed or cancelled bookings"):
        await service._load_booking(booking.id)


@pytest.mark.asyncio
async def test_delete_link_cancels_gateway_and_deletes():
    session = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    settings = MagicMock()
    gateway = MagicMock()
    gateway.cancel_link = AsyncMock(return_value={"IsSuccess": True})

    service = PaymentLinkService(session, settings, gateway)

    from app.payment_links.infrastructure.models import PaymentLink
    link = MagicMock(spec=PaymentLink)
    link.id = uuid.uuid4()
    link.target_type = "booking"
    link.target_id = uuid.uuid4()
    link.payment_url = "https://pay.test/123"
    link.gateway_invoice_id = "INV-999"

    service.get_link = AsyncMock(return_value=link)

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)

    await service.delete_link(link.id)

    gateway.cancel_link.assert_awaited_once_with("INV-999")
    session.delete.assert_awaited_once_with(link)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_and_delete_links_for_target():
    session = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    settings = MagicMock()
    gateway = MagicMock()
    gateway.cancel_link = AsyncMock(return_value={"IsSuccess": True})

    service = PaymentLinkService(session, settings, gateway)

    from app.payment_links.infrastructure.models import PaymentLink
    target_id = uuid.uuid4()
    link1 = MagicMock(spec=PaymentLink)
    link1.id = uuid.uuid4()
    link1.gateway_invoice_id = "INV-1"
    link2 = MagicMock(spec=PaymentLink)
    link2.id = uuid.uuid4()
    link2.gateway_invoice_id = "INV-2"

    booking = MagicMock(spec=Booking)
    booking.id = target_id
    booking.payment_url = "https://pay.test/1"

    # 1st execute returns links, 2nd execute returns booking
    exec_result_links = MagicMock()
    exec_result_links.scalars.return_value.all.return_value = [link1, link2]

    exec_result_booking = MagicMock()
    exec_result_booking.scalar_one_or_none.return_value = booking

    session.execute = AsyncMock(side_effect=[exec_result_links, exec_result_booking])

    count = await service.cancel_and_delete_links_for_target("booking", target_id)

    assert count == 2
    assert gateway.cancel_link.await_count == 2
    assert session.delete.await_count == 2
    assert booking.payment_url is None
    session.flush.assert_awaited_once()
