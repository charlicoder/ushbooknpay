"""Home-service bookings / home loyalty redemptions use the HOME loyalty points."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.booking.application.multi_service import _build_one
from app.booking.domain.services_snapshot import points_cost

DB_SERVICE = {
    "name": "Massage",
    "price_in_points": 100,
    "loyalty_points": 10,
    "home_price_in_points": 250,
    "home_loyalty_points": 25,
    "is_eligible_for_loyalty": True,
}
DB_ARR = {"arrangement_services": []}


def _item(service_data=None):
    return SimpleNamespace(
        service_id=uuid.uuid4(), service_name=None, service_category=None,
        service_data=service_data, service_arrangement_id=None, arrangement_name=None,
        arrangement_type=None, room_name=None, service_arrangement_data=None,
        base_price=None, base_duration=None, is_eligible_for_loyalty=False,
        loyalty_points=0, arrangement_loyalty_points=None, price_in_points=0,
        arrangement_price_in_points=None, package_id=None,
    )


def test_branch_booking_uses_regular_points():
    e = _build_one(_item(), 0, DB_SERVICE, DB_ARR, False)
    assert e["service_data"]["loyalty_points"] == 10
    assert points_cost(e) == 100


def test_home_booking_earns_home_points():
    e = _build_one(_item(), 0, DB_SERVICE, DB_ARR, False, is_home_service=True)
    assert e["service_data"]["loyalty_points"] == 25
    assert points_cost(e) == 250


def test_home_loyalty_redemption_flag_costs_home_points():
    e = _build_one(_item({"use_home_loyalty_points": True, "price_in_points": 1}), 0, DB_SERVICE, DB_ARR, True)
    assert points_cost(e) == 250


def test_loyalty_booking_does_not_create_payment_record():
    """Loyalty bookings (rewarded) must not create a payment record or set payment_id."""
    import inspect
    from app.api.v1 import bookings as bookings_router
    from app.booking.infrastructure.models import Booking
    from sqlalchemy import String

    col = Booking.__table__.c.payment_id
    assert isinstance(col.type, String)

    src = inspect.getsource(bookings_router.create_booking)
    # Payment creation block for loyalty is removed:
    assert "payment_provider=\"Loyalty Points\"" not in src


