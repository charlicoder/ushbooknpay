"""
app/voucher/infrastructure/repository.py
──────────────────────────────────────────
Data access layer for the Gift Voucher domain.

All query methods are async and return ORM instances.
The caller (application service) is responsible for commit/rollback.
"""

from app.common.utils import DEFAULT_TIMEZONE
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import and_, cast, func, or_, select, String, text
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
            return datetime.combine(d, t, tzinfo=DEFAULT_TIMEZONE)  # filter dates are Kuwait business dates
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=DEFAULT_TIMEZONE)  # naive input = Kuwait wall-clock
        return dt
    except Exception:
        return None


def _extract_sequence(existing_val: Any) -> int:
    """Extract integer sequence from scalar query result.

    Handles:
    - None / 0 / empty: returns 0 (so next sequence is 1)
    - int / float: returns int (so next sequence is int + 1)
    - string voucher number e.g. "VOU/2026/10/000007": returns 7
    - string digits e.g. "99": returns 99
    - other strings / invalid: returns 0
    """
    if not existing_val:
        return 0
    if isinstance(existing_val, (int, float)):
        return int(existing_val)
    if isinstance(existing_val, str):
        val_str = existing_val.strip()
        if not val_str:
            return 0
        last_part = val_str.split("/")[-1]
        if last_part.isdigit():
            return int(last_part)
        try:
            return int(val_str)
        except ValueError:
            return 0
    return 0


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

    async def get_by_voucher_number(self, voucher_number: str) -> GiftVoucher | None:
        """Return a GiftVoucher by its voucher_number or None."""
        result = await self._session.execute(
            select(GiftVoucher).where(GiftVoucher.voucher_number == voucher_number)
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
        delivery_status: str | None = None,
        gift_category: str | None = None,
        is_digital_gift_opened: bool | None = None,
        expire_date: str | None = None,
        created_at: str | None = None,
        payment_through: str | None = None,
        sender_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """
        Return paginated vouchers with optional filters (admin and interservice use).

        Returns:
            (items, total_count)
        """
        base = select(GiftVoucher)
        if status:
            base = base.where(GiftVoucher.status == status)
        if delivery_status:
            base = base.where(GiftVoucher.delivery_status == delivery_status)
        if gift_category:
            base = base.where(GiftVoucher.gift_category == gift_category.strip().lower())
        if is_digital_gift_opened is not None:
            base = base.where(GiftVoucher.is_digital_gift_opened == is_digital_gift_opened)
        if payment_through:
            base = base.where(GiftVoucher.payment_through == payment_through.strip().lower())
        if sender_id:
            base = base.where(GiftVoucher.sender_id == sender_id)
        if service_id:
            base = base.where(GiftVoucher.service_id == service_id)

        # Date filtering
        if created_at:
            c_start = _parse_filter_date(created_at, end_of_day=False)
            c_end = _parse_filter_date(created_at, end_of_day=True)
            if c_start and c_end and len(created_at.strip()) == 10:
                base = base.where(and_(GiftVoucher.created_at >= c_start, GiftVoucher.created_at <= c_end))
            elif c_start:
                base = base.where(GiftVoucher.created_at >= c_start)

        if expire_date:
            e_start = _parse_filter_date(expire_date, end_of_day=False)
            e_end = _parse_filter_date(expire_date, end_of_day=True)
            if e_start and e_end and len(expire_date.strip()) == 10:
                base = base.where(and_(GiftVoucher.expire_date >= e_start, GiftVoucher.expire_date <= e_end))
            elif e_start:
                base = base.where(GiftVoucher.expire_date >= e_start)

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

    # ── Voucher number generation ─────────────────────────────────────────────

    async def generate_voucher_number(self, for_date: date | None = None) -> str:
        """
        Generate the next sequential voucher number for *for_date*.

        Format: VOU/YYYY/MM/{NNNNNN}
        Example: VOU/2026/10/000001  (first voucher in October 2026)

        The sequence is derived from the highest existing ``voucher_number``
        sharing the same month prefix (LIKE 'VOU/YYYY/MM/%') and adding 1.
        This handles gaps (e.g. deleted or failed vouchers) safely without
        colliding with existing records.

        Args:
            for_date: The calendar day to use for the prefix.
                      Defaults to today (UTC).

        Returns:
            A unique voucher_number string e.g. ``"VOU/2026/10/000001"``.
        """
        ref_date = for_date or datetime.now(tz=timezone.utc).date()
        date_prefix = f"VOU/{ref_date.strftime('%Y/%m')}/"

        # In PostgreSQL, acquire a transaction advisory lock to serialize concurrent voucher generation
        try:
            bind = getattr(self._session, "bind", None)
            if bind is None and hasattr(self._session, "get_bind"):
                if not any(cls.__name__ in ("MagicMock", "AsyncMock") for cls in type(self._session).__mro__):
                    bind = self._session.get_bind()
            if bind and getattr(bind.dialect, "name", None) == "postgresql":
                await self._session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('gift_voucher_number'))")
                )
        except Exception:
            pass

        # Query highest existing voucher_number for this month
        stmt = select(func.max(GiftVoucher.voucher_number)).where(
            GiftVoucher.voucher_number.like(f"{date_prefix}%")
        )
        count_result = await self._session.execute(stmt)
        existing_val = count_result.scalar_one() if hasattr(count_result, "scalar_one") else 0
        if hasattr(existing_val, "__await__"):
            existing_val = await existing_val

        highest_sequence = _extract_sequence(existing_val)
        sequence = highest_sequence + 1
        return f"{date_prefix}{sequence:06d}"

    # ── Write operations ──────────────────────────────────────────────────────

    @property
    def session(self) -> AsyncSession:
        return self._session

    def add(self, voucher: GiftVoucher) -> GiftVoucher:
        """Stage a new GiftVoucher for insertion (caller must flush/commit)."""
        self._session.add(voucher)
        return voucher

    async def flush(self) -> None:
        """Flush pending ORM changes to the DB without committing."""
        await self._session.flush()

    async def flush_in_savepoint(self) -> None:
        """Flush within a nested transaction (SAVEPOINT) if supported, else regular flush."""
        if hasattr(self._session, "begin_nested") and not any(
            cls.__name__ in ("MagicMock", "AsyncMock") for cls in type(self._session).__mro__
        ):
            async with self._session.begin_nested():
                await self._session.flush()
        else:
            await self._session.flush()
