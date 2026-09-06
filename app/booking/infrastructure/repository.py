"""
app/booking/infrastructure/repository.py
─────────────────────────────────────────
BookingRepository — async persistence layer for the Booking domain.

Responsibilities:
- CRUD for Booking, BookingStatusHistory, TemporaryHold
- Optimised availability queries (batch, no N+1)
- Transactional locking for double-booking prevention
- No business logic — only data access
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.booking.domain.value_objects import BookingStatus
from app.booking.infrastructure.models import (
    Booking,
    BookingStatusHistory,
    TemporaryHold,
)
from app.common.utils import utcnow
from app.core.exceptions import BookingNotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BookingRepository:
    """Async repository for Booking aggregate."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Create / Update ───────────────────────────────────────────────────

    async def create(self, booking: Booking) -> Booking:
        """Persist a new booking. Does NOT commit — caller manages transaction."""
        self._session.add(booking)
        await self._session.flush()  # Populate server-generated fields (id, timestamps)
        await self._session.refresh(booking)
        logger.info("booking_created", booking_id=str(booking.id))
        return booking

    async def update(self, booking: Booking) -> Booking:
        """Persist changes to an existing booking. Does NOT commit."""
        await self._session.flush()
        await self._session.refresh(booking)
        return booking

    async def save_status_history(self, history: BookingStatusHistory) -> None:
        """Append a status history record within the current transaction."""
        self._session.add(history)
        await self._session.flush()

    # ── Retrieval ─────────────────────────────────────────────────────────

    async def get_by_id(
        self,
        booking_id: uuid.UUID,
        *,
        load_history: bool = False,
        for_update: bool = False,
    ) -> Booking:
        """
        Fetch a booking by primary key.

        Args:
            booking_id: UUID of the booking.
            load_history: Whether to eagerly load status_history.
            for_update: Acquire a row-level lock (FOR UPDATE).

        Raises:
            BookingNotFoundError: if no booking with this ID exists.
        """
        stmt = select(Booking).where(Booking.id == booking_id)
        if load_history:
            stmt = stmt.options(selectinload(Booking.status_history))
        if for_update:
            stmt = stmt.with_for_update()

        result = await self._session.execute(stmt)
        booking = result.scalar_one_or_none()
        if booking is None:
            raise BookingNotFoundError(f"Booking {booking_id} not found.")
        return booking

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> Booking | None:
        """Return an existing booking matching the idempotency key, or None."""
        stmt = select(Booking).where(Booking.idempotency_key == idempotency_key)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_bookings(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | BookingStatus | None = None,
        payment_status: str | None = None,
        date_str: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        customer_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        therapist_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        service_arrangement_id: uuid.UUID | None = None,
        search: str | None = None,
    ) -> tuple[list[Booking], int]:
        """
        Query bookings with multi-parameter filtering and pagination.
        """
        from datetime import date
        from sqlalchemy import cast, Date, func

        stmt = select(Booking)
        conditions = []

        if status:
            val = status.value if hasattr(status, "value") else str(status)
            conditions.append(Booking.status == val)

        if payment_status:
            val = payment_status.value if hasattr(payment_status, "value") else str(payment_status)
            conditions.append(Booking.payment_status == val)

        if customer_id:
            conditions.append(Booking.customer_id == customer_id)

        if branch_id:
            conditions.append(Booking.branch_id == branch_id)

        if therapist_id:
            conditions.append(Booking.therapist_id == therapist_id)

        if service_id:
            conditions.append(Booking.service_id == service_id)

        if service_arrangement_id:
            conditions.append(Booking.service_arrangement_id == service_arrangement_id)

        if date_str:
            try:
                d = date.fromisoformat(date_str.strip())
                conditions.append(cast(Booking.appointment_start, Date) == d)
            except Exception:
                pass

        if from_date:
            try:
                if len(from_date) == 10:
                    d_from = date.fromisoformat(from_date.strip())
                    conditions.append(cast(Booking.appointment_start, Date) >= d_from)
                else:
                    dt_from = datetime.fromisoformat(from_date.strip())
                    conditions.append(Booking.appointment_start >= dt_from)
            except Exception:
                pass

        if to_date:
            try:
                if len(to_date) == 10:
                    d_to = date.fromisoformat(to_date.strip())
                    conditions.append(cast(Booking.appointment_start, Date) <= d_to)
                else:
                    dt_to = datetime.fromisoformat(to_date.strip())
                    conditions.append(Booking.appointment_start <= dt_to)
            except Exception:
                pass

        if search:
            s = f"%{search.strip()}%"
            conditions.append(
                or_(
                    Booking.customer_notes.ilike(s),
                    Booking.internal_notes.ilike(s),
                    Booking.customer_data["name"].astext.ilike(s),
                    Booking.customer_data["first_name"].astext.ilike(s),
                    Booking.customer_data["last_name"].astext.ilike(s),
                    Booking.customer_data["email"].astext.ilike(s),
                    Booking.customer_data["phone"].astext.ilike(s),
                    Booking.customer_data["phone_number"].astext.ilike(s),
                    Booking.branch_data["branch_name"].astext.ilike(s),
                    Booking.service_data["service_name"].astext.ilike(s),
                    Booking.therapist_data["therapist_name"].astext.ilike(s),
                )
            )

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        count_result = await self._session.execute(count_stmt)
        total = count_result.scalar_one()

        # Paginated items
        stmt = (
            stmt.order_by(Booking.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all()), total

    async def list_by_customer(
        self,
        customer_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        status_filter: BookingStatus | str | None = None,
        payment_status_filter: str | None = None,
        date_str: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> tuple[list[Booking], int]:
        """
        List bookings for a customer with optional filters.
        """
        return await self.list_bookings(
            page=page,
            page_size=page_size,
            customer_id=customer_id,
            status=status_filter,
            payment_status=payment_status_filter,
            date_str=date_str,
            from_date=from_date,
            to_date=to_date,
        )

    # ── Availability Query Support ────────────────────────────────────────

    async def get_active_bookings_for_therapist_in_range(
        self,
        therapist_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> list[Booking]:
        """
        Return all non-cancelled bookings for a therapist within the date range.

        Used by the availability engine — optimised for batch loading.
        """
        stmt = (
            select(Booking)
            .where(
                and_(
                    Booking.therapist_id == therapist_id,
                    Booking.appointment_start < end,
                    Booking.appointment_end > start,
                    Booking.status.notin_(
                        [BookingStatus.CANCELLED.value, BookingStatus.NO_SHOW.value]
                    ),
                )
            )
            .order_by(Booking.appointment_start)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_bookings_for_therapists_in_range(
        self,
        therapist_ids: list[uuid.UUID],
        start: datetime,
        end: datetime,
    ) -> list[Booking]:
        """
        Batch version — fetch bookings for multiple therapists at once.

        This prevents N+1 queries in the availability engine.
        """
        if not therapist_ids:
            return []

        stmt = (
            select(Booking)
            .where(
                and_(
                    Booking.therapist_id.in_(therapist_ids),
                    Booking.appointment_start < end,
                    Booking.appointment_end > start,
                    Booking.status.notin_(
                        [BookingStatus.CANCELLED.value, BookingStatus.NO_SHOW.value]
                    ),
                )
            )
            .order_by(Booking.therapist_id, Booking.appointment_start)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def check_therapist_overlap(
        self,
        therapist_id: uuid.UUID,
        start: datetime,
        end: datetime,
        exclude_booking_id: uuid.UUID | None = None,
    ) -> bool:
        """
        Return True if there is any active booking overlapping [start, end) for the therapist.

        Called during booking creation inside a locked transaction to prevent double-booking.
        """
        stmt = select(Booking.id).where(
            and_(
                Booking.therapist_id == therapist_id,
                Booking.appointment_start < end,
                Booking.appointment_end > start,
                Booking.status.notin_(
                    [BookingStatus.CANCELLED.value, BookingStatus.NO_SHOW.value]
                ),
            )
        )
        if exclude_booking_id:
            stmt = stmt.where(Booking.id != exclude_booking_id)

        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_busy_therapist_ids_in_range(
        self,
        therapist_ids: list[uuid.UUID],
        start: datetime,
        end: datetime,
    ) -> set[uuid.UUID]:
        """
        Efficient single query returning set of therapist IDs that have conflicting active bookings or holds in [start, end).
        """
        if not therapist_ids:
            return set()

        # 1. Conflicting active bookings
        stmt_bookings = (
            select(Booking.therapist_id)
            .where(
                and_(
                    Booking.therapist_id.in_(therapist_ids),
                    Booking.appointment_start < end,
                    Booking.appointment_end > start,
                    Booking.status.notin_(
                        [BookingStatus.CANCELLED.value, BookingStatus.NO_SHOW.value]
                    ),
                )
            )
            .distinct()
        )
        res_bookings = await self._session.execute(stmt_bookings)
        busy_ids: set[uuid.UUID] = set(res_bookings.scalars().all())

        # 2. Conflicting unexpired temporary holds
        now = utcnow()
        stmt_holds = (
            select(TemporaryHold.therapist_id)
            .where(
                and_(
                    TemporaryHold.therapist_id.in_(therapist_ids),
                    TemporaryHold.appointment_start < end,
                    TemporaryHold.appointment_end > start,
                    TemporaryHold.expires_at > now,
                )
            )
            .distinct()
        )
        res_holds = await self._session.execute(stmt_holds)
        busy_ids.update(res_holds.scalars().all())

        return busy_ids

    # ── Temporary Holds ───────────────────────────────────────────────────

    async def create_hold(self, hold: TemporaryHold) -> TemporaryHold:
        """Persist a temporary hold."""
        self._session.add(hold)
        await self._session.flush()
        return hold

    async def delete_hold(self, booking_id: uuid.UUID) -> None:
        """Remove any temporary hold for the given booking."""
        stmt = delete(TemporaryHold).where(
            TemporaryHold.booking_id == booking_id
        )
        await self._session.execute(stmt)

    async def get_active_holds_for_therapist_in_range(
        self,
        therapist_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> list[TemporaryHold]:
        """Return non-expired holds for a therapist in the given range."""
        now = utcnow()
        stmt = select(TemporaryHold).where(
            and_(
                TemporaryHold.therapist_id == therapist_id,
                TemporaryHold.appointment_start < end,
                TemporaryHold.appointment_end > start,
                TemporaryHold.expires_at > now,
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def cleanup_expired_holds(self) -> int:
        """Delete all expired holds. Returns the number of rows deleted."""
        stmt = delete(TemporaryHold).where(
            TemporaryHold.expires_at <= utcnow()
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]
