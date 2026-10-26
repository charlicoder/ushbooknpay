"""
app/payment_links/application/link_service.py
──────────────────────────────────────────────
Creates / reads / cancels hosted payment links for bookings and gift vouchers.

The :class:`PaymentLink` row is the single source of truth for a link:

  1. insert  → status ``created`` with the API request body (``request_data``)
  2. gateway → ``gateway_request`` / ``gateway_response`` / ``payment_url`` /
               ``gateway_invoice_id`` are stored and the status becomes ``pending``
  3. webhook → status ``paid`` / ``failed`` (see ``settlement.py``)

Creating a link NEVER creates a ``payments`` row (that happens only after the
payment succeeded), NEVER changes the booking / voucher status and NEVER
triggers customer messages.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.domain.value_objects import BookingStatus, PaymentStatus
from app.booking.infrastructure.models import Booking
from app.core.config import Settings
from app.core.exceptions import NotFoundError, PaymentProviderError, ValidationError
from app.core.logging import get_logger
from app.payment_links.domain.gateway import PaymentLinkGateway, PaymentLinkRequest
from app.payment_links.infrastructure.models import (
    PaymentLink,
    PaymentLinkStatus,
    PaymentLinkTarget,
)
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import GiftVoucherStatus
from app.voucher.infrastructure.models import GiftVoucher

logger = get_logger(__name__)

TargetType = Literal["booking", "voucher", "gift_voucher"]

#: statuses in which a link can still be paid by the customer
OPEN_STATUSES = (
    PaymentLinkStatus.CREATED.value,
    PaymentLinkStatus.PENDING.value,
    PaymentLinkStatus.FAILED.value,
)
_PAID_BOOKING_STATUSES = {
    PaymentStatus.SUCCESS.value,
    PaymentStatus.REFUNDED.value,
    PaymentStatus.PARTIALLY_REFUNDED.value,
    PaymentStatus.REWARDED.value,
}
_BOOKING_LINKABLE = (
    BookingStatus.CONFIRMED.value,
    BookingStatus.PAYMENT_PENDING.value,
    BookingStatus.DRAFT.value,
    BookingStatus.REQUESTED.value,
    BookingStatus.PAYMENT_FAILED.value,
    BookingStatus.CANCELLED.value,
    "confirmed",
    "payment_pending",
    "draft",
    "requested",
    "payment_failed",
    "cancelled",
)


def normalise_target_type(target_type: str) -> str:
    """``voucher`` is accepted as an alias of the canonical ``gift_voucher``."""
    return PaymentLinkTarget.GIFT_VOUCHER.value if target_type == "voucher" else target_type


def _first(data: dict[str, Any] | None, *keys: str) -> str | None:
    for key in keys:
        val = (data or {}).get(key)
        if val not in (None, ""):
            return str(val)
    return None


def _contact(data: dict[str, Any] | None) -> tuple[str, str | None, str | None]:
    name = _first(data, "name", "full_name")
    if not name:
        name = " ".join(p for p in (_first(data, "first_name"), _first(data, "last_name")) if p)
    return (
        name or "Customer",
        _first(data, "phone_number", "phone", "mobile"),
        _first(data, "email"),
    )


class PaymentLinkService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        gateway: PaymentLinkGateway,
    ) -> None:
        self._session = session
        self._settings = settings
        self._gateway = gateway
        self._settlement = None  # created lazily (sync flow only)

    # ── Queries ─────────────────────────────────────────────────────────
    async def get_link(self, link_id: uuid.UUID) -> PaymentLink:
        link = (
            await self._session.execute(select(PaymentLink).where(PaymentLink.id == link_id))
        ).scalar_one_or_none()
        if link is None:
            raise NotFoundError("Payment link not found.")
        return link

    async def list_for_target(self, target_type: str, target_id: uuid.UUID) -> list[PaymentLink]:
        stmt = (
            select(PaymentLink)
            .where(
                PaymentLink.target_type == normalise_target_type(target_type),
                PaymentLink.target_id == target_id,
            )
            .order_by(PaymentLink.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    @staticmethod
    def is_expired(link: PaymentLink) -> bool:
        return bool(link.expires_at and link.expires_at <= datetime.now(timezone.utc))

    # ── Create ──────────────────────────────────────────────────────────
    async def create_link(
        self,
        *,
        target_type: TargetType,
        target_id: uuid.UUID,
        amount: Decimal | None = None,
        actor_id: str,
        actor_data: dict[str, Any] | None = None,
        invoice_id: str | None = None,
        invoice_number: str | None = None,
        force_new: bool = False,
        expiry_minutes: int | None = None,
        request_data: dict[str, Any] | None = None,
    ) -> PaymentLink:
        canonical = normalise_target_type(target_type)
        if canonical == PaymentLinkTarget.BOOKING.value:
            target = await self._load_booking(target_id)
        elif canonical == PaymentLinkTarget.GIFT_VOUCHER.value:
            target = await self._load_voucher(target_id)
        else:
            raise ValidationError(f"Payment links are not supported for '{target_type}' yet.")

        if amount is not None:
            resolved_amount = Decimal(str(amount))
        elif canonical == PaymentLinkTarget.BOOKING.value and target.status in (BookingStatus.CANCELLED.value, "cancelled"):
            p_data = target.payment_data or target.payments_data or {}
            ref_amt = None
            if isinstance(p_data, dict):
                ref_amt = p_data.get("refund_amount") or p_data.get("refunded_amount")
            if ref_amt is not None:
                resolved_amount = Decimal(str(ref_amt))
            else:
                resolved_amount = Decimal(str(target.total_amount))
        else:
            resolved_amount = Decimal(str(target.total_amount))

        if resolved_amount <= 0:
            raise ValidationError("Cannot create a payment link for a zero amount.")
        amount = resolved_amount

        existing = await self._find_open_link(canonical, target_id)
        if existing is not None:
            reusable = (
                existing.payment_url
                and not self.is_expired(existing)
                and existing.gateway == self._gateway.name
                and Decimal(str(existing.amount)) == amount
            )
            if reusable and not force_new:
                return existing

            # If amount changed or new link requested:
            # 1. Send cancel request to gateway for previous paymentlink
            if existing.gateway_invoice_id:
                try:
                    await self._gateway.cancel_link(existing.gateway_invoice_id)
                except Exception as exc:
                    logger.info("gateway_cancel_link_failed", error=str(exc))
            # 2. Delete paymentlink record for this booking
            await self._session.delete(existing)
            await self._session.flush()

        if invoice_id is not None and not str(invoice_id).strip():
            invoice_id = None
        if invoice_number is not None and not str(invoice_number).strip():
            invoice_number = None

        invoice_id, invoice_number = await self._resolve_invoice(
            canonical, target, invoice_id, invoice_number
        )
        is_booking = canonical == PaymentLinkTarget.BOOKING.value
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=expiry_minutes or self._settings.PAYMENT_LINK_EXPIRY_MINUTES
        )

        # 1) record the request before talking to the gateway
        link = PaymentLink(
            link_number=f"PL-{datetime.now(timezone.utc):%y%m%d}-{secrets.token_hex(3).upper()}",
            target_type=canonical,
            target_id=target_id,
            target_ref=(target.booking_number if is_booking else target.voucher_number),
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            gateway=self._gateway.name,
            provider_label=self._gateway.provider_label,
            amount=amount,
            currency=target.currency or "KWD",
            status=PaymentLinkStatus.CREATED.value,
            customer_id=target.customer_id if is_booking else target.sender_id,
            customer_data=target.customer_data if is_booking else target.sender_data,
            request_data={
                **(request_data or {}),
                "target_type": canonical,
                "target_id": str(target_id),
                "invoice_id": invoice_id,
                "invoice_number": invoice_number,
                "force_new": force_new,
                "expiry_minutes": expiry_minutes,
            },
            created_by=actor_id,
            created_by_data=actor_data,
            expires_at=expires_at,
        )
        self._session.add(link)
        await self._session.flush()

        # 2) call the gateway and store its request/response on the same record
        name, phone, email = self._payer_contact(canonical, target)
        try:
            result = await self._gateway.create_link(
                PaymentLinkRequest(
                    amount=amount,
                    currency=link.currency,
                    reference=str(link.id),
                    description=self._description(canonical, target),
                    customer_name=name,
                    customer_phone=phone,
                    customer_email=email,
                    expires_at=expires_at,
                )
            )
        except PaymentProviderError as exc:
            link.status = PaymentLinkStatus.FAILED.value
            link.failure_data = {
                "stage": "create_link",
                "error": str(exc),
                "code": getattr(exc, "code", None),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            await self._session.commit()  # keep the failed attempt for support/debug
            raise

        link.gateway_invoice_id = result.gateway_invoice_id
        link.payment_url = result.payment_url
        link.gateway_request = result.raw_request
        link.gateway_response = result.raw_response
        link.status = PaymentLinkStatus.PENDING.value
        await self._attach_link_to_target(canonical, target, link, actor_id)
        await self._session.flush()
        logger.info(
            "payment_link_created",
            link_id=str(link.id),
            target_type=canonical,
            target_id=str(target_id),
            invoice_number=invoice_number,
            gateway=self._gateway.name,
        )
        return link

    # ── Renew ───────────────────────────────────────────────────────────
    async def renew_link(
        self,
        link_id: uuid.UUID,
        *,
        actor_id: str,
        actor_data: dict[str, Any] | None = None,
        expiry_minutes: int | None = None,
    ) -> PaymentLink:
        """Issue a fresh link for an unpaid link whose validity has run out.

        Only unpaid links that are expired (by ``expires_at`` or status), cancelled
        or failed can be renewed. The previous link is cancelled at the gateway
        (best effort) and replaced by a new one for the same target and amount.
        """
        old = await self.get_link(link_id)
        if old.status in (
            PaymentLinkStatus.PAID.value,
            PaymentLinkStatus.REFUNDED.value,
            PaymentLinkStatus.PARTIALLY_REFUNDED.value,
        ):
            raise ValidationError("This payment link is already paid and cannot be renewed.")
        renewable = (
            old.status in (PaymentLinkStatus.EXPIRED.value, PaymentLinkStatus.CANCELLED.value, PaymentLinkStatus.FAILED.value)
            or self.is_expired(old)
        )
        if not renewable:
            raise ValidationError("This payment link is still active and does not need to be renewed.")
        return await self.create_link(
            target_type=old.target_type,
            target_id=old.target_id,
            amount=Decimal(str(old.amount)),
            actor_id=actor_id,
            actor_data=actor_data,
            invoice_id=old.invoice_id,
            invoice_number=old.invoice_number,
            force_new=True,
            expiry_minutes=expiry_minutes,
            request_data={"renewed_from": str(old.id)},
        )

    # ── Sync (pull) ─────────────────────────────────────────────────────
    async def sync_status(self, link_id: uuid.UUID) -> PaymentLink:
        """Pull the state from the gateway and settle if it was paid.

        Safety net next to the webhook (the gateway docs recommend relying on both).
        """
        settlement = self._settlement_service()
        link = await settlement.get_locked_link(link_id)
        if link is None:
            raise NotFoundError("Payment link not found.")
        if link.status not in OPEN_STATUSES or not link.gateway_invoice_id:
            return link
        state = await self._gateway.get_payment_state(link.gateway_invoice_id)
        await settlement.apply(link, state, source="manual_sync")
        await self._session.flush()
        return link

    def _settlement_service(self):
        from app.payment_links.application.settlement import PaymentSettlementService

        if self._settlement is None:
            self._settlement = PaymentSettlementService(self._session, self._settings)
        return self._settlement

    async def publish_pending_events(self) -> None:
        """Publish SQS events queued by :meth:`sync_status`. Call AFTER the commit."""
        if self._settlement is not None:
            await self._settlement.publish_pending_events()

    # ── Cancel ──────────────────────────────────────────────────────────
    async def cancel_link(self, link_id: uuid.UUID) -> PaymentLink:
        link = await self.get_link(link_id)
        if link.status not in OPEN_STATUSES:
            raise ValidationError(f"Only unpaid links can be cancelled (current status: {link.status}).")
        if link.gateway_invoice_id:
            try:
                await self._gateway.cancel_link(link.gateway_invoice_id)
            except Exception as exc:
                logger.info("gateway_cancel_link_failed", error=str(exc))
        self._mark_cancelled(link, reason="Payment link cancelled by staff")
        await self._session.flush()
        return link

    # ── Delete ──────────────────────────────────────────────────────────
    async def delete_link(self, link_id: uuid.UUID) -> None:
        """Send cancel request to gateway and delete the payment link record."""
        link = await self.get_link(link_id)
        if link.gateway_invoice_id:
            try:
                await self._gateway.cancel_link(link.gateway_invoice_id)
            except Exception as exc:
                logger.info("gateway_cancel_link_failed", error=str(exc))

        if link.target_type == PaymentLinkTarget.BOOKING.value:
            booking = (
                await self._session.execute(
                    select(Booking).where(Booking.id == link.target_id).with_for_update()
                )
            ).scalar_one_or_none()
            if booking and booking.payment_url == link.payment_url:
                booking.payment_url = None

        await self._session.delete(link)
        await self._session.flush()
        logger.info("payment_link_deleted", link_id=str(link_id))

    async def cancel_and_delete_links_for_target(
        self, target_type: str, target_id: uuid.UUID
    ) -> int:
        """Send cancel request to gateway for all links for this target and delete records."""
        canonical = normalise_target_type(target_type)
        stmt = (
            select(PaymentLink)
            .where(PaymentLink.target_type == canonical, PaymentLink.target_id == target_id)
            .with_for_update()
        )
        links = (await self._session.execute(stmt)).scalars().all()
        count = 0
        for link in links:
            if link.gateway_invoice_id:
                try:
                    await self._gateway.cancel_link(link.gateway_invoice_id)
                except Exception as exc:
                    logger.info("gateway_cancel_link_failed", link_id=str(link.id), error=str(exc))
            await self._session.delete(link)
            count += 1

        if canonical == PaymentLinkTarget.BOOKING.value:
            booking = (
                await self._session.execute(
                    select(Booking).where(Booking.id == target_id).with_for_update()
                )
            ).scalar_one_or_none()
            if booking:
                existing_pdata = dict(booking.payment_data or {})
                cancelled_invoices = list(existing_pdata.get("cancelled_invoice_ids", []))
                for link in links:
                    if link.gateway_invoice_id and str(link.gateway_invoice_id) not in cancelled_invoices:
                        cancelled_invoices.append(str(link.gateway_invoice_id))

                gw_inv = existing_pdata.get("gateway_invoice_id") or existing_pdata.get("invoice_id")
                if gw_inv and str(gw_inv) not in cancelled_invoices:
                    cancelled_invoices.append(str(gw_inv))
                    try:
                        await self._gateway.cancel_link(str(gw_inv))
                    except Exception as exc:
                        logger.info("gateway_cancel_link_failed", invoice_id=str(gw_inv), error=str(exc))

                booking.payment_url = None
                existing_pdata["cancelled_invoice_ids"] = cancelled_invoices
                existing_pdata["payment_link_cancelled_at"] = datetime.now(timezone.utc).isoformat()
                booking.payment_data = existing_pdata

        await self._session.flush()
        logger.info("payment_links_deleted_for_target", target_type=canonical, target_id=str(target_id), count=count)
        return count

    @staticmethod
    def _mark_cancelled(link: PaymentLink, *, reason: str) -> None:
        link.status = PaymentLinkStatus.CANCELLED.value
        link.meta = {**(link.meta or {}), "cancel_reason": reason, "cancelled_at": datetime.now(timezone.utc).isoformat()}

    # ── Internals ───────────────────────────────────────────────────────
    async def _find_open_link(self, target_type: str, target_id: uuid.UUID) -> PaymentLink | None:
        stmt = (
            select(PaymentLink)
            .where(
                PaymentLink.target_type == target_type,
                PaymentLink.target_id == target_id,
                PaymentLink.status.in_(OPEN_STATUSES),
            )
            .order_by(PaymentLink.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _load_booking(self, booking_id: uuid.UUID) -> Booking:
        booking = (
            await self._session.execute(select(Booking).where(Booking.id == booking_id).with_for_update())
        ).scalar_one_or_none()
        if booking is None:
            raise NotFoundError("Booking not found.")
        status_val = str(booking.status).lower() if booking.status else ""
        if booking.status not in _BOOKING_LINKABLE and status_val not in _BOOKING_LINKABLE:
            raise ValidationError(
                f"A payment link cannot be created for a booking in '{booking.status}' status."
            )
        if status_val not in (BookingStatus.CANCELLED.value, "cancelled"):
            if booking.payment_status in _PAID_BOOKING_STATUSES or (
                booking.payment_status and str(booking.payment_status).lower() in _PAID_BOOKING_STATUSES
            ):
                raise ValidationError("This booking is already paid.")
        return booking

    async def _load_voucher(self, voucher_id: uuid.UUID) -> GiftVoucher:
        voucher = (
            await self._session.execute(select(GiftVoucher).where(GiftVoucher.id == voucher_id).with_for_update())
        ).scalar_one_or_none()
        if voucher is None:
            raise NotFoundError("Gift voucher not found.")
        allowed = (GiftVoucherStatus.CREATED.value, GiftVoucherStatus.PAYMENT_PENDING.value)
        if voucher.status not in allowed:
            raise ValidationError(
                f"A payment link can only be created for unpaid vouchers (current: {voucher.status})."
            )
        return voucher

    async def _resolve_invoice(
        self,
        target_type: str,
        target: Booking | GiftVoucher,
        invoice_id: str | None,
        invoice_number: str | None,
    ) -> tuple[str | None, str | None]:
        """Invoice from the request → the booking → accounting (ushanr), cheapest first."""
        if target_type != PaymentLinkTarget.BOOKING.value:
            return invoice_id, invoice_number
        invoice_number = invoice_number or getattr(target, "invoice_number", None)
        if invoice_id and invoice_number:
            return invoice_id, invoice_number
        found = None
        try:
            from app.integrations.ushanr_client import UshanrClient

            client = UshanrClient()
            try:
                # Only look up an already existing invoice; NEVER auto-create an invoice for an unpaid/pending booking
                existing = await client.get_invoice_by_source("booking", str(target.id))
                if existing and existing.get("invoice_name"):
                    found = {
                        "invoice_id": existing.get("invoice_id"),
                        "invoice_name": existing.get("invoice_name"),
                    }
                    if not getattr(target, "invoice_number", None):
                        target.invoice_number = existing["invoice_name"]
                        self._session.add(target)
            finally:
                await client.aclose()
        except Exception as exc:  # accounting must never block link creation
            logger.warning("payment_link_invoice_lookup_failed", error=str(exc))
            found = None
        if found:
            invoice_id = invoice_id or (str(found["invoice_id"]) if found.get("invoice_id") else None)
            invoice_number = invoice_number or found.get("invoice_name")
        return invoice_id, invoice_number

    @staticmethod
    def _payer_contact(target_type: str, target: Booking | GiftVoucher) -> tuple[str, str | None, str | None]:
        if target_type == PaymentLinkTarget.BOOKING.value:
            return _contact(target.customer_data)
        return _contact(target.sender_data)

    @staticmethod
    def _description(target_type: str, target: Booking | GiftVoucher) -> str:
        if target_type == PaymentLinkTarget.BOOKING.value:
            service = (target.service_data or {}).get("name") or (target.service_data or {}).get("name_en")
            ref = target.booking_number or str(target.id)[:8]
            is_cancel = target.status in (BookingStatus.CANCELLED.value, "cancelled")
            prefix = "Refund for Booking" if is_cancel else "Booking"
            return f"{prefix} {ref}" + (f" - {service}" if service else "")
        return f"Gift voucher {target.voucher_number or str(target.id)[:8]}"

    async def _attach_link_to_target(
        self, target_type: str, target: Booking | GiftVoucher, link: PaymentLink, actor_id: str
    ) -> None:
        """Expose the link on the target; the target's status is NOT changed for bookings."""
        target.payment_url = link.payment_url
        target.payment_provider = link.provider_label
        if target_type == PaymentLinkTarget.BOOKING.value:
            return
        if target.status == GiftVoucherStatus.CREATED.value:
            # CREATED → PAYMENT_PENDING (emits the pending event; ushnotice sends nothing for it)
            await GiftVoucherService(self._session).update_status(
                target.id,
                GiftVoucherStatus.PAYMENT_PENDING.value,
                payment_url=link.payment_url,
                payment_provider=link.provider_label,
                actor_id=actor_id,
            )
