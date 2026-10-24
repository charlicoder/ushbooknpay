"""
tests/unit/booking/test_multi_service_booking.py
────────────────────────────────────────────────
Multi-service booking: several services (same branch + therapist) per booking.

Covers schema normalisation (legacy ↔ ``services``), snapshot helpers, the
concurrent entry resolver, the service layer (legacy columns mirror services[0]),
the REST create endpoint and the SQS event payload.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.api.v1.bookings import _booking_to_detail, _services_out, create_booking
from app.booking.application.multi_service import (
    ServiceValidationError,
    resolve_service_entries,
)
from app.booking.application.services import BookingService, _build_booking_event_data
from app.booking.domain.services_snapshot import (
    build_service_entry,
    distinct_arrangement_ids,
    effective_points,
    get_booking_services,
    joined_service_name,
    points_cost,
    primary_fields,
    total_services_duration,
    total_services_price,
)
from app.booking.domain.value_objects import PricingBreakdown
from app.booking.infrastructure.models import Booking
from app.booking.interfaces.schemas import CreateBookingRequest
from app.events.contracts import BookingConfirmedEvent, BookingCreatedEvent

S1, S2, S3 = (str(uuid.uuid4()) for _ in range(3))
A1, A2 = str(uuid.uuid4()), str(uuid.uuid4())


def _item(**kw):
    return kw


# ── Schema ────────────────────────────────────────────────────────────────────


def test_legacy_payload_is_wrapped_into_single_service():
    body = CreateBookingRequest(
        service_id=S1,
        service_arrangement_id=A1,
        base_price="40.00",
        base_duration=60,
        service_data={"name": "Massage"},
    )
    assert len(body.services) == 1
    only = body.services[0]
    assert str(only.service_id) == S1
    assert str(only.service_arrangement_id) == A1
    assert only.service_data == {"name": "Massage"}
    assert only.base_duration == 60


def test_camel_case_services_payload_and_flat_backfill():
    body = CreateBookingRequest.model_validate(
        {
            "services": [
                {"serviceId": S1, "serviceArrangementId": A1, "basePrice": "30", "baseDuration": 45},
                {"serviceId": S2, "serviceArrangementId": A2, "basePrice": "20", "baseDuration": 30},
            ],
            "therapistId": str(uuid.uuid4()),
        }
    )
    assert [str(s.service_id) for s in body.services] == [S1, S2]
    # legacy flat fields mirror services[0]
    assert str(body.service_id) == S1
    assert str(body.service_arrangement_id) == A1
    assert body.base_duration == 45


def test_duplicate_service_arrangement_pair_is_rejected():
    with pytest.raises(ValidationError):
        CreateBookingRequest(
            services=[
                {"service_id": S1, "service_arrangement_id": A1},
                {"service_id": S1, "service_arrangement_id": A1},
            ]
        )


def test_same_service_in_different_arrangements_is_allowed():
    body = CreateBookingRequest(
        services=[
            {"service_id": S1, "service_arrangement_id": A1},
            {"service_id": S1, "service_arrangement_id": A2},
        ]
    )
    assert len(body.services) == 2


def test_explicit_service_without_id_is_rejected():
    with pytest.raises(ValidationError):
        CreateBookingRequest(services=[{"service_arrangement_id": A1}])


def test_more_than_ten_services_is_rejected():
    with pytest.raises(ValidationError):
        CreateBookingRequest(services=[{"service_id": str(uuid.uuid4())} for _ in range(11)])


def test_empty_services_list_falls_back_to_legacy_fields():
    with pytest.raises(ValidationError):
        CreateBookingRequest(services=[])


def test_oversized_snapshot_is_rejected():
    with pytest.raises(ValidationError):
        CreateBookingRequest(
            services=[{"service_id": S1, "service_data": {"blob": "x" * 40_000}}]
        )


# ── Snapshot helpers ──────────────────────────────────────────────────────────


def _entry(sid, aid=None, name="X", price="10", dur=30, **sd):
    return build_service_entry(
        service_id=sid,
        service_data={"name": name, **sd},
        service_arrangement_id=aid,
        service_arrangement_data={"arrangement_name": f"Room-{name}"},
        base_price=price,
        duration_minutes=dur,
    )


def test_build_entry_formats_money_and_ids():
    e = _entry(S1, A1, price="12.5")
    assert e["service_id"] == S1
    assert e["service_arrangement_id"] == A1
    assert e["base_price"] == "12.500"


def test_totals_names_and_distinct_arrangements():
    entries = [_entry(S1, A1, "A", "10", 30), _entry(S2, A1, "B", "5.5", 20), _entry(S3, A2, "C", "1", 10)]
    assert total_services_price(entries) == Decimal("16.500")
    assert total_services_duration(entries) == 60
    assert joined_service_name(entries) == "A, B, C"
    assert distinct_arrangement_ids(entries) == [A1, A2]


def test_effective_points_prefers_arrangement_override():
    e = _entry(S1, A1, loyalty_points=5, price_in_points=100)
    e["service_arrangement_data"].update({"loyalty_points": 9, "price_in_points": None})
    assert effective_points(e) == (9, 100)
    assert points_cost(e) == 100


def test_get_booking_services_falls_back_to_legacy_columns():
    legacy = MagicMock(spec=Booking)
    legacy.services = None
    legacy.service_id = uuid.UUID(S1)
    legacy.service_data = {"name": "Old"}
    legacy.service_arrangement_id = uuid.UUID(A1)
    legacy.service_arrangement_data = {"name": "Room"}
    legacy.base_price = Decimal("7")
    legacy.duration_minutes = 45
    entries = get_booking_services(legacy)
    assert len(entries) == 1
    assert entries[0]["service_id"] == S1
    assert entries[0]["duration_minutes"] == 45
    assert entries[0]["base_price"] == "7.000"


def test_primary_fields_mirror_first_entry():
    p = primary_fields([_entry(S1, A1), _entry(S2, A2)])
    assert p["service_id"] == uuid.UUID(S1)
    assert p["service_arrangement_id"] == uuid.UUID(A1)


# ── Resolver ──────────────────────────────────────────────────────────────────


def _body(items):
    return CreateBookingRequest(services=items)


@pytest.mark.asyncio
async def test_resolver_fetches_each_id_once_and_builds_entries():
    ushauth = AsyncMock()
    ushauth.get_service = AsyncMock(
        side_effect=lambda sid: {"data": {"name": f"svc-{sid[:4]}", "base_price": "10", "duration_minutes": 30}}
    )
    ushauth.get_service_arrangement = AsyncMock(
        return_value={
            "data": {
                "name": "Room",
                "branch_name": "Main",
                "arrangement_services": [
                    {"service_id": S1, "is_active": True, "loyalty_points": 3},
                    {"service_id": S2, "is_active": True},
                ],
            }
        }
    )
    body = _body(
        [
            {"service_id": S1, "service_arrangement_id": A1},
            {"service_id": S2, "service_arrangement_id": A1},
        ]
    )
    entries = await resolve_service_entries(ushauth, body.services)

    assert ushauth.get_service.await_count == 2
    assert ushauth.get_service_arrangement.await_count == 1  # shared arrangement fetched once
    assert [e["position"] for e in entries] == [0, 1]
    assert entries[0]["service_arrangement_data"]["loyalty_points"] == 3
    assert total_services_duration(entries) == 60


@pytest.mark.asyncio
async def test_resolver_rejects_service_not_in_arrangement():
    ushauth = AsyncMock()
    ushauth.get_service = AsyncMock(return_value={"data": {"name": "s"}})
    ushauth.get_service_arrangement = AsyncMock(
        return_value={"data": {"branch_name": "Main", "arrangement_services": [{"service_id": S1, "is_active": True}]}}
    )
    body = _body(
        [
            {"service_id": S1, "service_arrangement_id": A1},
            {"service_id": S2, "service_arrangement_id": A1},
        ]
    )
    with pytest.raises(ServiceValidationError):
        await resolve_service_entries(ushauth, body.services)


@pytest.mark.asyncio
async def test_resolver_rejects_arrangements_of_different_branches():
    ushauth = AsyncMock()
    ushauth.get_service = AsyncMock(return_value={"data": {"name": "s"}})

    async def _arr(aid):
        return {"data": {"branch_name": "Main" if aid == A1 else "Other", "arrangement_services": []}}

    ushauth.get_service_arrangement = AsyncMock(side_effect=_arr)
    body = _body(
        [
            {"service_id": S1, "service_arrangement_id": A1},
            {"service_id": S2, "service_arrangement_id": A2},
        ]
    )
    with pytest.raises(ServiceValidationError):
        await resolve_service_entries(ushauth, body.services)


@pytest.mark.asyncio
async def test_resolver_survives_upstream_failures():
    ushauth = AsyncMock()
    ushauth.get_service = AsyncMock(side_effect=RuntimeError("down"))
    ushauth.get_service_arrangement = AsyncMock(side_effect=RuntimeError("down"))
    body = _body([{"service_id": S1, "service_arrangement_id": A1, "service_name": "Spa", "base_duration": 30}])
    entries = await resolve_service_entries(ushauth, body.services)
    assert entries[0]["service_data"]["name"] == "Spa"


@pytest.mark.asyncio
async def test_resolver_loyalty_redemption_never_earns_points():
    ushauth = AsyncMock()
    ushauth.get_service = AsyncMock(
        return_value={"data": {"loyalty_points": 10, "is_eligible_for_loyalty": True, "price_in_points": 50}}
    )
    ushauth.get_service_arrangement = AsyncMock(return_value={"data": {}})
    body = _body([{"service_id": S1}])
    entries = await resolve_service_entries(ushauth, body.services, is_loyalty_booking=True)
    sd = entries[0]["service_data"]
    assert sd["loyalty_points"] == 0 and sd["is_eligible_for_loyalty"] is False
    assert sd["price_in_points"] == 50


# ── Service layer ─────────────────────────────────────────────────────────────


def _pricing(total="60.000"):
    return PricingBreakdown(
        arrangement_price=Decimal(total),
        price_for_extra_minutes=Decimal("0"),
        addon_price=Decimal("0"),
        discount=Decimal("0"),
        tax=Decimal("0"),
        fees=Decimal("0"),
        total=Decimal(total),
        currency="KWD",
    )


def _service():
    svc = BookingService(session=AsyncMock())
    svc._repo.get_by_idempotency_key = AsyncMock(return_value=None)
    svc._repo.check_therapist_overlap = AsyncMock(return_value=False)
    svc._repo.generate_booking_number = AsyncMock(return_value="BOK/2026/10/000001")
    svc._repo.create = AsyncMock(side_effect=lambda b: b)
    svc._repo.create_hold = AsyncMock()
    svc._record_status_change = AsyncMock()
    svc._enqueue_event = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_service_create_booking_stores_services_and_mirrors_primary():
    svc = _service()
    entries = [
        _entry(S1, A1, "Massage", "40", 60, loyalty_points=4),
        _entry(S2, A1, "Facial", "20", 30, loyalty_points=2),
    ]
    booking = await svc.create_booking(
        customer_id=uuid.uuid4(),
        services=entries,
        therapist_id=uuid.uuid4(),
        appointment_start=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 20, 11, 30, tzinfo=timezone.utc),
        duration_minutes=90,
        pricing=_pricing(),
        status="confirmed",
        payment_status="success",
    )
    assert booking.services == entries
    assert booking.service_id == uuid.UUID(S1)  # legacy columns mirror services[0]
    assert booking.service_arrangement_id == uuid.UUID(A1)
    assert booking.service_data["name"] == "Massage"

    events = [c[0][0] for c in svc._enqueue_event.call_args_list]
    created = next(e for e in events if isinstance(e, BookingCreatedEvent))
    assert created.service_name == "Massage, Facial"
    assert [s["service_id"] for s in created.services] == [S1, S2]

    confirmed = next(e for e in events if isinstance(e, BookingConfirmedEvent))
    assert [s["service_name"] for s in confirmed.services] == ["Massage", "Facial"]
    assert confirmed.service_name == "Massage, Facial"
    assert confirmed.service_id == S1  # primary preserved for old consumers
    assert confirmed.loyalty_points == 6  # aggregated across services


@pytest.mark.asyncio
async def test_service_create_booking_legacy_arguments_still_work():
    svc = _service()
    booking = await svc.create_booking(
        customer_id=uuid.uuid4(),
        service_id=uuid.UUID(S1),
        service_data={"name": "Solo"},
        service_arrangement_id=uuid.UUID(A1),
        therapist_id=uuid.uuid4(),
        appointment_start=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        pricing=_pricing("40.000"),
    )
    assert len(booking.services) == 1
    assert booking.services[0]["service_id"] == S1
    assert booking.services[0]["duration_minutes"] == 60


@pytest.mark.asyncio
async def test_service_create_booking_requires_a_service():
    svc = _service()
    with pytest.raises(ValueError):
        await svc.create_booking(
            customer_id=uuid.uuid4(),
            therapist_id=uuid.uuid4(),
            appointment_start=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
            appointment_end=datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc),
            duration_minutes=60,
            pricing=_pricing(),
        )


# ── Event payload / responses ─────────────────────────────────────────────────


def _orm_booking(entries):
    b = Booking(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        therapist_id=uuid.uuid4(),
        services=entries,
        **primary_fields(entries),
        appointment_date=datetime(2026, 9, 20, tzinfo=timezone.utc),
        appointment_start=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        appointment_end=datetime(2026, 9, 20, 11, 30, tzinfo=timezone.utc),
        duration_minutes=90,
        extra_minutes=0,
        arrangement_price=Decimal("60"),
        price_for_extra_minutes=Decimal("0"),
        addon_price=Decimal("0"),
        discount=Decimal("0"),
        tax=Decimal("0"),
        fees=Decimal("0"),
        total_amount=Decimal("60"),
        currency="KWD",
        status="confirmed",
        payment_status="success",
        booking_type="branch_service",
        payment_type="service",
        customer_data={},
        branch_data={},
        therapist_data={},
        payment_data={},
        addons=[],
    )
    return b


def test_event_data_aggregates_points_and_lists_services():
    entries = [
        _entry(S1, A1, "A", "10", 30, loyalty_points=3, price_in_points=30, is_eligible_for_loyalty=True),
        _entry(S2, A2, "B", "10", 30, loyalty_points=2, price_in_points=20),
    ]
    data = _build_booking_event_data(_orm_booking(entries))
    assert data["service_name"] == "A, B"
    assert data["loyalty_points"] == 5
    assert data["price_in_points"] == 50
    assert data["arrangement_loyalty_points"] is None
    assert data["is_eligible_for_loyalty"] is True
    assert [s["service_arrangement_name"] for s in data["services"]] == ["Room-A", "Room-B"]


def test_event_data_single_service_keeps_legacy_semantics():
    data = _build_booking_event_data(_orm_booking([_entry(S1, A1, "Solo", loyalty_points=3)]))
    assert data["service_name"] == "Solo"
    assert data["loyalty_points"] == 3
    assert len(data["services"]) == 1


def test_detail_and_list_responses_expose_services():
    b = _orm_booking([_entry(S1, A1, "A"), _entry(S2, A2, "B")])
    out = _services_out(b)
    assert [s.service_id for s in out] == [S1, S2]
    assert [s.position for s in out] == [0, 1]
    b.created_at = b.updated_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    detail = _booking_to_detail(b)
    assert len(detail.services) == 2
    assert detail.service_id == S1  # legacy field = primary


# ── REST endpoint ─────────────────────────────────────────────────────────────


def _api_fixtures(tid, available="yes"):
    mock_service = MagicMock(spec=BookingService)
    created = MagicMock(spec=Booking)
    created.id = uuid.uuid4()
    created.customer_id = uuid.uuid4()
    created.total_amount = Decimal("60.000")
    created.status = "requested"
    created.payment_status = "not_initiated"
    created.payment_type = "service"
    created.payment_data = {}
    created.service_data = {}
    created.services = None
    created.service_id = None
    created.loyalty_data = None
    created.reward_id = None
    created.voucher_id = None
    created.voucher_data = None
    mock_service.create_booking = AsyncMock(return_value=created)

    ushauth = AsyncMock()
    ushauth.check_appointment_availability = AsyncMock(
        return_value={"available": available, "therapist_id": str(tid)}
    )
    ushauth.get_customer_profile = AsyncMock(return_value={"data": {"user": {}}})
    ushauth.get_service = AsyncMock(
        side_effect=lambda sid: {"data": {"name": f"svc-{sid[:4]}", "duration_minutes": 30}}
    )
    ushauth.get_service_arrangement = AsyncMock(
        return_value={"data": {"branch_name": "Main", "arrangement_services": []}}
    )
    req = MagicMock(spec=Request)
    req.headers = Headers()
    user = MagicMock()
    user.sub = str(uuid.uuid4())
    return mock_service, ushauth, req, user


@pytest.mark.asyncio
async def test_api_create_multi_service_booking_passes_services_and_total_window():
    tid = uuid.uuid4()
    mock_service, ushauth, req, user = _api_fixtures(tid)
    body = CreateBookingRequest(
        services=[
            {"service_id": S1, "service_arrangement_id": A1, "base_price": "40", "base_duration": 60},
            {"service_id": S2, "service_arrangement_id": A2, "base_price": "20", "base_duration": 30},
        ],
        branch_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-09-20",
        appointment_time="10:00:00",
    )
    resp = await create_booking(req, body, user, mock_service, ushauth, MagicMock())

    kwargs = mock_service.create_booking.call_args.kwargs
    entries = kwargs["services"]
    assert [e["service_id"] for e in entries] == [S1, S2]
    assert kwargs["duration_minutes"] == 90  # sum of service durations
    assert kwargs["pricing"].arrangement_price == Decimal("60")  # sum of service prices
    assert kwargs["base_price"] == Decimal("60")
    assert (kwargs["appointment_end"] - kwargs["appointment_start"]).total_seconds() == 90 * 60

    # availability checked for the primary AND the additional arrangement (same therapist)
    called_arrs = {c.kwargs["service_arrangement_id"] for c in ushauth.check_appointment_availability.await_args_list}
    assert called_arrs == {A1, A2}
    assert resp.success is True


@pytest.mark.asyncio
async def test_api_create_multi_service_rejects_unavailable_additional_arrangement():
    tid = uuid.uuid4()
    mock_service, ushauth, req, user = _api_fixtures(tid)

    async def _avail(**kw):
        if kw["service_arrangement_id"] == A2:
            return {"available": "no"}
        return {"available": "yes", "therapist_id": str(tid)}

    ushauth.check_appointment_availability = AsyncMock(side_effect=_avail)
    body = CreateBookingRequest(
        services=[
            {"service_id": S1, "service_arrangement_id": A1, "base_duration": 60},
            {"service_id": S2, "service_arrangement_id": A2, "base_duration": 30},
        ],
        branch_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-09-20",
        appointment_time="10:00:00",
    )
    with pytest.raises(HTTPException) as exc:
        await create_booking(req, body, user, mock_service, ushauth, MagicMock())
    assert exc.value.status_code == 409
    mock_service.create_booking.assert_not_called()


@pytest.mark.asyncio
async def test_api_create_multi_service_never_reserves_less_than_sum_of_durations():
    tid = uuid.uuid4()
    mock_service, ushauth, req, user = _api_fixtures(tid)
    body = CreateBookingRequest(
        services=[
            {"service_id": S1, "service_arrangement_id": A1, "base_duration": 60},
            {"service_id": S2, "service_arrangement_id": A1, "base_duration": 60},
        ],
        total_duration=30,  # client claims less than the services need
        branch_id=uuid.uuid4(),
        therapist_id=tid,
        appointment_date="2026-09-20",
        appointment_time="10:00:00",
    )
    await create_booking(req, body, user, mock_service, ushauth, MagicMock())
    kwargs = mock_service.create_booking.call_args.kwargs
    assert (kwargs["appointment_end"] - kwargs["appointment_start"]).total_seconds() == 120 * 60


@pytest.mark.asyncio
async def test_api_create_legacy_single_service_payload_unchanged():
    tid = uuid.uuid4()
    mock_service, ushauth, req, user = _api_fixtures(tid)
    body = CreateBookingRequest(
        service_id=S1,
        service_arrangement_id=A1,
        branch_id=uuid.uuid4(),
        therapist_id=tid,
        base_price="40.000",
        base_duration=60,
        appointment_date="2026-09-20",
        appointment_time="10:00:00",
    )
    await create_booking(req, body, user, mock_service, ushauth, MagicMock())
    kwargs = mock_service.create_booking.call_args.kwargs
    assert len(kwargs["services"]) == 1
    assert kwargs["duration_minutes"] == 60
    assert kwargs["pricing"].arrangement_price == Decimal("40.000")
    assert ushauth.check_appointment_availability.await_count == 1
