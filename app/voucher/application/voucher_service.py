"""
app/voucher/application/voucher_service.py
───────────────────────────────────────────
Gift Voucher application service — all business logic and orchestration.

Responsibilities:
  - Create new gift vouchers
  - Validate and execute status transitions via GiftVoucherStateMachine
  - Emit SQS domain events on status changes (best-effort, non-blocking)
  - Provide read methods for customer-facing and admin endpoints

SQS events emitted:
  voucher.active          — fired when status transitions to "active"
  voucher.payment_pending — fired when status transitions to "payment_pending"
  voucher.redeemed        — fired when status transitions to "redeemed"
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.events.contracts import (
    VoucherActiveEvent,
    VoucherPaymentPendingEvent,
    VoucherRedeemedEvent,
)
from app.events.sqs_client import get_sqs_client
from app.voucher.domain.state_machine import GiftVoucherStateMachine
from app.voucher.domain.value_objects import (
    GiftVoucherStatus,
    VoucherPaymentProvider,
    VoucherPaymentThrough,
)
from app.voucher.infrastructure.models import GiftVoucher
from app.voucher.infrastructure.repository import GiftVoucherRepository

logger = get_logger(__name__)


class GiftVoucherService:
    """
    Orchestrates all gift-voucher operations.

    All methods operate within the caller-managed AsyncSession.
    The caller is responsible for committing or rolling back the transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = GiftVoucherRepository(session)

    # ── Creation ──────────────────────────────────────────────────────────────

    async def create_voucher(
        self,
        *,
        service_id: uuid.UUID,
        service_data: dict[str, Any],
        total_amount: Decimal,
        sender_id: uuid.UUID,
        sender_data: dict[str, Any],
        branch_id: uuid.UUID | None = None,
        branch_data: dict[str, Any] | None = None,
        service_arrangement_id: uuid.UUID | None = None,
        service_arrangement_data: dict[str, Any] | None = None,
        addons: list[dict[str, Any]] | None = None,
        extra_time: int = 0,
        price_for_extra_time: Decimal | None = None,
        total_duration: int = 0,
        currency: str = "KWD",
        recipient_phone: str | None = None,
        recipient_id: uuid.UUID | None = None,
        recipient_data: dict[str, Any] | None = None,
        gift_message: str | None = None,
        gift_template: str | None = None,
        booking_id: uuid.UUID | None = None,
        booking_data: dict[str, Any] | None = None,
        created_by: uuid.UUID | None = None,
        payment_id: str | None = None,
        payment_data: dict[str, Any] | None = None,
        payment_url: str | None = None,
        payment_provider: str | None = None,
        payment_through: str | None = None,
        status: str | None = None,
        expire_date: datetime | None = None,
    ) -> GiftVoucher:
        """
        Create a new GiftVoucher.

        A unique secret_code and public_token are auto-generated on the model.

        Args:
            service_id:               External service UUID.
            service_data:             Snapshot of service metadata.
            total_amount:             Amount to be charged for the voucher.
            sender_id:                UUID of the customer purchasing the voucher.
            sender_data:              {"name": ..., "phone_number": ...}
            branch_id:                Optional branch UUID.
            branch_data:              Snapshot of branch metadata.
            service_arrangement_id:   Optional arrangement UUID.
            service_arrangement_data: Snapshot of arrangement metadata.
            addons:                   List of add-on snapshots.
            extra_time:               Extra time in minutes.
            price_for_extra_time:     Price per extra-time unit (decimal, nullable).
            total_duration:           Total service duration in minutes.
            currency:                 Currency code (default KWD).
            recipient_phone:          Recipient's phone number for delivery.
            recipient_id:             Optional UUID of recipient in ushauth.
            recipient_data:           {"name": ..., "email": ..., "phone_number": ...}
            gift_message:             Optional personalised message.
            gift_template:            Optional template identifier.
            booking_id:               Optional booking that triggered creation.
            booking_data:             Optional booking snapshot that triggered creation.
            created_by:               Optional staff UUID (for admin-created vouchers).
            payment_id:               Optional gateway transaction reference (e.g. '100624710000000255').
            payment_data:             Optional full gateway response snapshot (JSONB).
            payment_url:              Optional payment gateway checkout URL.
            payment_provider:         Payment gateway used (MyFatoorah, DirectLink, Deema, Other).
            payment_through:          Sales channel (ushspa, desk).
            status:                   Initial status ('created', 'payment_pending', 'active'). Defaults to 'created'.
            expire_date:              Optional explicit expiration timestamp. Defaults to +60 days.

        Returns:
            The newly created, flushed GiftVoucher ORM instance.
        """
        initial_status = status or GiftVoucherStatus.CREATED.value
        payment_provider = VoucherPaymentProvider.normalise(payment_provider)
        payment_through = VoucherPaymentThrough.normalise(payment_through)
        voucher = GiftVoucher(
            service_id=service_id,
            service_data=service_data,
            branch_id=branch_id,
            branch_data=branch_data or {},
            service_arrangement_id=service_arrangement_id,
            service_arrangement_data=service_arrangement_data or {},
            addons=addons or [],
            extra_time=extra_time,
            price_for_extra_time=price_for_extra_time,
            total_duration=total_duration,
            total_amount=total_amount,
            currency=currency,
            sender_id=sender_id,
            sender_data=sender_data,
            recipient_phone=recipient_phone,
            recipient_id=recipient_id,
            recipient_data=recipient_data or {},
            gift_message=gift_message,
            gift_template=gift_template,
            booking_id=booking_id,
            booking_data=booking_data or {},
            created_by=created_by,
            payment_id=payment_id,
            payment_data=payment_data,
            payment_url=payment_url,
            payment_provider=payment_provider,
            payment_through=payment_through,
            status=initial_status,
        )
        if expire_date is not None:
            voucher.expire_date = expire_date

        if initial_status == GiftVoucherStatus.REDEEMED.value:
            voucher.redeemed_at = datetime.now(tz=timezone.utc)
            voucher.redeemed_by = created_by

        self._repo.add(voucher)
        await self._repo.flush()

        logger.info(
            "gift_voucher_created",
            voucher_id=str(voucher.id),
            sender_id=str(sender_id),
            service_id=str(service_id),
            amount=str(total_amount),
            status=initial_status,
            payment_provider=payment_provider,
            payment_through=payment_through,
        )

        if initial_status == GiftVoucherStatus.ACTIVE.value:
            asyncio.create_task(
                self._emit_status_event(voucher, GiftVoucherStatus.ACTIVE)
            )

        return voucher

    # ── Status transitions ────────────────────────────────────────────────────

    async def update_status(
        self,
        voucher_id: uuid.UUID,
        new_status: str,
        *,
        payment_id: str | None = None,
        payment_data: dict[str, Any] | None = None,
        payment_url: str | None = None,
        booking_id: uuid.UUID | None = None,
        booking_data: dict[str, Any] | None = None,
        payment_provider: str | None = None,
        payment_through: str | None = None,
        redeemed_by: uuid.UUID | None = None,
        actor_id: str | None = None,
    ) -> GiftVoucher:
        """
        Transition a voucher's status, setting ancillary fields as appropriate.

        Validates the transition via GiftVoucherStateMachine.
        Emits SQS events for: active, payment_pending, redeemed.

        Args:
            voucher_id:       UUID of the voucher to update.
            new_status:       Target status string (must be a GiftVoucherStatus value).
            payment_id:       Gateway payment reference string (e.g. "100624710000000255").
                              Set when activating a voucher after payment success.
            payment_data:     Full payment provider response snapshot (JSONB).
                              Stored alongside payment_id for audit.
            payment_url:      Payment gateway redirect/checkout URL.
            booking_id:       Set when redeeming — the booking using the voucher.
            booking_data:     Snapshot of booking data when redeeming or updating status.
            payment_provider: Payment gateway used (MyFatoorah, DirectLink, Deema, Other).
            payment_through:  Sales channel (ushspa, desk).
            redeemed_by:      UUID of the user redeeming the voucher (auto-set to API requester).
            actor_id:         Optional string identifier of who made the change (for logs).

        Returns:
            The updated GiftVoucher.

        Raises:
            NotFoundError:   if the voucher does not exist.
            ValidationError: if the status transition is illegal.
        """
        voucher = await self._repo.get_by_id(voucher_id)
        if voucher is None:
            raise NotFoundError(f"Gift voucher {voucher_id} not found.")

        try:
            current_status = GiftVoucherStatus(voucher.status)
            target_status = GiftVoucherStatus(new_status)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        try:
            GiftVoucherStateMachine.validate_transition(current_status, target_status)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        old_status = voucher.status
        voucher.status = target_status.value

        # ── Set ancillary fields on key transitions ──────────────────────
        if payment_url is not None:
            voucher.payment_url = payment_url

        if payment_provider is not None:
            voucher.payment_provider = VoucherPaymentProvider.normalise(payment_provider)

        if payment_through is not None:
            voucher.payment_through = VoucherPaymentThrough.normalise(payment_through)

        if target_status == GiftVoucherStatus.ACTIVE:
            # Store gateway reference string and full response snapshot
            if payment_id is not None:
                voucher.payment_id = payment_id
            if payment_data is not None:
                voucher.payment_data = payment_data

        if target_status == GiftVoucherStatus.REDEEMED:
            # Auto-update redeemed_at timestamp and redeemed_by user
            voucher.redeemed_at = datetime.now(tz=timezone.utc)
            if booking_id:
                voucher.redeemed_booking_id = booking_id
            if redeemed_by is not None:
                voucher.redeemed_by = redeemed_by

        if booking_data is not None:
            voucher.booking_data = booking_data

        await self._repo.flush()

        logger.info(
            "gift_voucher_status_updated",
            voucher_id=str(voucher_id),
            old_status=old_status,
            new_status=target_status.value,
            actor_id=actor_id,
        )

        # ── Emit SQS event (fire-and-forget, best-effort) ────────────────
        if old_status != target_status.value:
            asyncio.create_task(
                self._emit_status_event(voucher, target_status)
            )

        return voucher


    # ── Read operations ───────────────────────────────────────────────────────

    async def get_by_id(self, voucher_id: uuid.UUID) -> GiftVoucher:
        """
        Return a GiftVoucher by ID.

        Raises:
            NotFoundError: if not found.
        """
        voucher = await self._repo.get_by_id(voucher_id)
        if voucher is None:
            raise NotFoundError(f"Gift voucher {voucher_id} not found.")
        return voucher

    async def get_by_public_token(self, public_token: str) -> GiftVoucher:
        """
        Return a GiftVoucher by its public URL token.

        Raises:
            NotFoundError: if not found.
        """
        voucher = await self._repo.get_by_public_token(public_token)
        if voucher is None:
            raise NotFoundError("Gift voucher not found.")
        return voucher

    async def list_my_vouchers(
        self,
        sender_id: uuid.UUID,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """Return paginated vouchers sent by *sender_id*."""
        return await self._repo.list_by_sender(
            sender_id, status=status, page=page, page_size=page_size
        )

    async def list_all(
        self,
        *,
        status: str | None = None,
        sender_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[GiftVoucher], int]:
        """Return paginated vouchers with optional admin filters."""
        return await self._repo.list_all(
            status=status,
            sender_id=sender_id,
            service_id=service_id,
            page=page,
            page_size=page_size,
        )

    async def list_vouchers_for_recipient(
        self,
        recipient_phone: str,
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
        Return paginated vouchers where recipient mobile matches *recipient_phone*.
        Supports filtering by status, created_at, date ranges, service, branch, etc.
        """
        return await self._repo.list_by_recipient_phone(
            recipient_phone,
            status=status,
            created_at=created_at,
            from_date=from_date,
            to_date=to_date,
            service_id=service_id,
            branch_id=branch_id,
            available_only=available_only,
            page=page,
            page_size=page_size,
        )

    async def list_sent_vouchers(
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
        Return paginated vouchers **sent by** *sender_id* (i.e. I am the sender).

        Supports the full filter set (status, date range, service, branch) to
        match the /my-sent-vouchers/ endpoint.

        Args:
            sender_id:  UUID of the authenticated customer (from JWT sub).
            status:     Optional status filter string.
            created_at: Exact date string (YYYY-MM-DD) to filter on created_at.
            from_date:  Lower-bound date/datetime for created_at.
            to_date:    Upper-bound date/datetime for created_at.
            service_id: Optional service UUID filter.
            branch_id:  Optional branch UUID filter.
            page:       1-indexed page number.
            page_size:  Maximum items per page.

        Returns:
            (items, total_count)
        """
        return await self._repo.list_sent_by_sender(
            sender_id,
            status=status,
            created_at=created_at,
            from_date=from_date,
            to_date=to_date,
            service_id=service_id,
            branch_id=branch_id,
            page=page,
            page_size=page_size,
        )

    # ── Internal: event emission ──────────────────────────────────────────────

    async def _emit_status_event(
        self,
        voucher: GiftVoucher,
        status: GiftVoucherStatus,
    ) -> None:
        """
        Publish the appropriate SQS domain event for *status*.

        This is called as a background task (asyncio.create_task) to avoid
        blocking the HTTP response. Errors are caught and logged but NOT
        re-raised so they cannot corrupt the DB transaction.
        """
        snapshot = voucher.to_snapshot()

        try:
            sqs = get_sqs_client()

            if status == GiftVoucherStatus.ACTIVE:
                event = VoucherActiveEvent(**snapshot)
            elif status == GiftVoucherStatus.PAYMENT_PENDING:
                event = VoucherPaymentPendingEvent(**snapshot)
            elif status == GiftVoucherStatus.REDEEMED:
                event = VoucherRedeemedEvent(**snapshot)
            else:
                # No SQS event for other transitions (cancelled, expired, etc.)
                return

            await sqs.publish_event(event)

        except Exception as exc:
            logger.warning(
                "gift_voucher_sqs_event_failed",
                voucher_id=snapshot.get("id"),
                status=status.value,
                error=str(exc),
            )
