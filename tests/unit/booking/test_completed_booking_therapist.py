import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException
from app.api.v1.bookings import (
    _is_admin_user,
    change_session_therapist,
    update_booking,
)
from app.booking.domain.value_objects import BookingStatus
from app.booking.infrastructure.models import Booking



def test_is_admin_user():
    admin_user = MagicMock()
    admin_user.is_superuser = True
    assert _is_admin_user(admin_user) is True

    admin_role = MagicMock()
    admin_role.is_superuser = False
    admin_role.role = "Administrator"
    assert _is_admin_user(admin_role) is True

    admin_type = MagicMock()
    admin_type.is_superuser = False
    admin_type.role = "staff"
    admin_type.user_type = "Admin"
    assert _is_admin_user(admin_type) is True

    regular_user = MagicMock()
    regular_user.is_superuser = False
    regular_user.user_type = "receptionist"
    regular_user.role = "Receptionist"
    regular_user.codenames = ["bookings.view"]
    regular_user.permissions = {}
    assert _is_admin_user(regular_user) is False


@pytest.mark.asyncio
async def test_non_admin_cannot_change_therapist_on_completed_booking():
    booking_id = uuid.uuid4()
    session_id = "s-1"
    booking = MagicMock(spec=Booking)
    booking.id = booking_id
    booking.status = BookingStatus.COMPLETED.value

    booking_service = AsyncMock()
    booking_service.get_booking = AsyncMock(return_value=booking)

    ushauth = AsyncMock()
    current_user = MagicMock()
    current_user.is_superuser = False
    current_user.user_type = "receptionist"
    current_user.role = "receptionist"
    current_user.codenames = []
    current_user.permissions = {}

    from app.api.v1.bookings import ChangeSessionTherapistRequest
    body = ChangeSessionTherapistRequest(therapist_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await change_session_therapist(
            booking_id=booking_id,
            session_id=session_id,
            body=body,
            current_user=current_user,
            booking_service=booking_service,
            ushauth=ushauth,
        )

    assert exc_info.value.status_code == 403
    assert "Only administrators" in exc_info.value.detail


@pytest.mark.asyncio
async def test_admin_can_change_therapist_on_completed_booking():
    booking_id = uuid.uuid4()
    session_id = "s-1"
    new_therapist_id = uuid.uuid4()
    booking = MagicMock(spec=Booking)
    booking.id = booking_id
    booking.status = BookingStatus.COMPLETED.value
    booking.therapist_id = new_therapist_id

    booking_service = AsyncMock()
    booking_service.get_booking = AsyncMock(return_value=booking)
    booking_service.change_session_therapist = AsyncMock(return_value=booking)

    ushauth = AsyncMock()
    ushauth.get_therapist = AsyncMock(return_value={"data": {"id": str(new_therapist_id), "name": "Jane"}})
    ushauth.update_appointment_cache_by_booking_id = AsyncMock()

    admin_user = MagicMock()
    admin_user.is_superuser = True
    admin_user.user_type = "admin"
    admin_user.role = "Administrator"

    from app.api.v1.bookings import ChangeSessionTherapistRequest
    body = ChangeSessionTherapistRequest(therapist_id=new_therapist_id)

    from unittest.mock import patch
    with patch("app.api.v1.bookings._booking_to_detail") as mock_detail:
        mock_detail.return_value.model_dump.return_value = {"id": str(booking_id)}
        response = await change_session_therapist(
            booking_id=booking_id,
            session_id=session_id,
            body=body,
            current_user=admin_user,
            booking_service=booking_service,
            ushauth=ushauth,
        )

    assert response.status_code == 200
    booking_service.change_session_therapist.assert_awaited_once()
