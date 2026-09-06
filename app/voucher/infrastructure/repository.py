"""
app/voucher/infrastructure/repository.py
──────────────────────────────────────────
Data access layer for the Gift Voucher domain.

All query methods are async and return ORM instances.
The caller (application service) is responsible for commit/rollback.
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.voucher.domain.value_objects import GiftVoucherStatus
from app.voucher.infrastructure.models import GiftVoucher


def get_phone_variants(phone: str) -> list[str]:
    """Generate common formatting variants of a phone number for matching."""
    if not phone:
        return []
    cleaned = phone.strip()
    digits = "".join(c for c in cleaned if c.isdigit())
    if not digits:
        return [cleaned]

    variants = {cleaned, digits, f"+{digits}", f"00{digits}"}

    # Normalize if starts with 00
    norm_digits = digits[2:] if digits.startswith("00") else digits

    # Handle Kuwait country code 965
    if norm_digits.startswith("965") and len(norm_digits) > 3:
        local = norm_digits[3:]
        variants.add(local)
        variants.add(f"+965{local}")
        variants.add(f"965{local}")
        variants.add(f"00965{local}")
    elif len(norm_digits) == 8:
        # 8-digit Kuwait local number
        variants.add(f"+965{norm_digits}")
        variants.add(f"965{norm_digits}")
        variants.add(f"00965{norm_digits}")

    return [v for v in variants if v]


def _parse_filter_date(val: str | None, end_of_day: bool = False) -> datetime | None:
    if not val:
        return None
    s = val.strip()
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            d = date.fromisoformat(s)
            t = time.max if end_of_day else time.min
            return datetime.combine(d, t, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class GiftVoucherRepository:
    """All database operations for GiftVoucher records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Single-record lookups ─────────────────────────────────────────────────

    async def get_by_id(self, voucher_id: uuid.UUID) -> GiftVoucher | None:
        """Return a GiftVoucher by primary key or None."""
        result = await self._session.execute(
            select(GiftVoucher).where(GiftVoucher.id == voucher_id)
        )
        return result.scalar_one_or_none()

    async def get_by_public_token(self, public_token: str) -> GiftVoucher | None:
        """Return a GiftVoucher by its public page token or None."""
        result = await self._session.execute(
            select(GiftVoucher).where(GiftVoucher.public_token == public_token)
        )
        return result.scalar_one_or_none()

    # ── Collection queries ────────────────────────────────────────────────────

    async def list_by_sender(
        self,
        sender_id: uuid.UUID,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """
        Return paginated vouchers created by *sender_id*.

        Returns:
            (items, total_count)
        """
        base = select(GiftVoucher).where(GiftVoucher.sender_id == sender_id)
        if status:
            base = base.where(GiftVoucher.status == status)

        count_result = await self._session.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar_one()

        stmt = (
            base
            .order_by(GiftVoucher.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = await self._session.execute(stmt)
        return rows.scalars().all(), total

    async def list_sent_by_sender(
        self,
        sender_id: uuid.UUID,
        *,
        status: str | None = None,
        created_at: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        service_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """
        Return paginated vouchers where *sender_id* matches, with optional
        date-range, service, and branch filters.

        This is the full-featured version used by the /my-sent-vouchers/ endpoint.
        ``list_by_sender`` is kept as the lightweight form for internal use.

        Returns:
            (items, total_count)
        """
        base = select(GiftVoucher).where(GiftVoucher.sender_id == sender_id)

        # Status filter
        if status:
            st = status.strip().lower()
            if st != "all":
                base = base.where(GiftVoucher.status == st)

        # Exact-date filter (takes precedence over from_date/to_date for that day)
        if created_at:
            c_start = _parse_filter_date(created_at, end_of_day=False)
            c_end = _parse_filter_date(created_at, end_of_day=True)
            if c_start and c_end and len(created_at.strip()) == 10:
                base = base.where(
                    and_(GiftVoucher.created_at >= c_start, GiftVoucher.created_at <= c_end)
                )
            elif c_start:
                base = base.where(GiftVoucher.created_at >= c_start)

        if from_date:
            f_dt = _parse_filter_date(from_date, end_of_day=False)
            if f_dt:
                base = base.where(GiftVoucher.created_at >= f_dt)

        if to_date:
            t_dt = _parse_filter_date(to_date, end_of_day=True)
            if t_dt:
                base = base.where(GiftVoucher.created_at <= t_dt)

        if service_id:
            base = base.where(GiftVoucher.service_id == service_id)
        if branch_id:
            base = base.where(GiftVoucher.branch_id == branch_id)

        count_result = await self._session.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar_one()

        stmt = (
            base
            .order_by(GiftVoucher.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = await self._session.execute(stmt)
        return rows.scalars().all(), total

    async def list_all(
        self,
        *,
        status: str | None = None,
        sender_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """
        Return paginated vouchers with optional filters (admin use).

        Returns:
            (items, total_count)
        """
        base = select(GiftVoucher)
        if status:
            base = base.where(GiftVoucher.status == status)
        if sender_id:
            base = base.where(GiftVoucher.sender_id == sender_id)
        if service_id:
            base = base.where(GiftVoucher.service_id == service_id)

        count_result = await self._session.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar_one()

        stmt = (
            base
            .order_by(GiftVoucher.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = await self._session.execute(stmt)
        return rows.scalars().all(), total

    async def list_by_recipient_phone(
        self,
        phone: str,
        *,
        status: str | None = None,
        created_at: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        service_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        available_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """
        Return paginated vouchers where recipient mobile matches *phone*.
        Supports filtering by status, created_at, date ranges, service, branch, etc.
        """
        variants = get_phone_variants(phone)
        phone_conditions = [
            GiftVoucher.recipient_phone.in_(variants),
            GiftVoucher.recipient_data["phone_number"].astext.in_(variants),
        ]

        digits = "".join(c for c in phone if c.isdigit())
        norm_digits = digits[2:] if digits.startswith("00") else digits
        local = norm_digits[3:] if norm_digits.startswith("965") and len(norm_digits) > 3 else norm_digits
        if len(local) >= 8:
            phone_conditions.append(GiftVoucher.recipient_phone.ilike(f"%{local}"))
            phone_conditions.append(GiftVoucher.recipient_data["phone_number"].astext.ilike(f"%{local}"))

        base = select(GiftVoucher).where(or_(*phone_conditions))

        # Status filtering
        if status:
            st = status.strip().lower()
            if st == "available":
                base = base.where(
                    and_(
                        GiftVoucher.status == GiftVoucherStatus.ACTIVE.value,
                        or_(GiftVoucher.expire_date.is_(None), GiftVoucher.expire_date > func.now()),
                    )
                )
            elif st != "all":
                base = base.where(GiftVoucher.status == st)
        elif available_only:
            base = base.where(
                and_(
                    GiftVoucher.status == GiftVoucherStatus.ACTIVE.value,
                    or_(GiftVoucher.expire_date.is_(None), GiftVoucher.expire_date > func.now()),
                )
            )

        # Date filtering
        if created_at:
            c_start = _parse_filter_date(created_at, end_of_day=False)
            c_end = _parse_filter_date(created_at, end_of_day=True)
            if c_start and c_end and len(created_at.strip()) == 10:
                base = base.where(and_(GiftVoucher.created_at >= c_start, GiftVoucher.created_at <= c_end))
            elif c_start:
                base = base.where(GiftVoucher.created_at >= c_start)

        if from_date:
            f_dt = _parse_filter_date(from_date, end_of_day=False)
            if f_dt:
                base = base.where(GiftVoucher.created_at >= f_dt)

        if to_date:
            t_dt = _parse_filter_date(to_date, end_of_day=True)
            if t_dt:
                base = base.where(GiftVoucher.created_at <= t_dt)

        if service_id:
            base = base.where(GiftVoucher.service_id == service_id)
        if branch_id:
            base = base.where(GiftVoucher.branch_id == branch_id)

        count_result = await self._session.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar_one()

        stmt = (
            base
            .order_by(GiftVoucher.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = await self._session.execute(stmt)
        return rows.scalars().all(), total

    # ── Write operations ──────────────────────────────────────────────────────

    def add(self, voucher: GiftVoucher) -> GiftVoucher:
        """Stage a new GiftVoucher for insertion (caller must flush/commit)."""
        self._session.add(voucher)
        return voucher

    async def flush(self) -> None:
        """Flush pending ORM changes to the DB without committing."""
        await self._session.flush()
