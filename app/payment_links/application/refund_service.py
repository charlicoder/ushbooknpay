"""
app/payment_links/application/refund_service.py
────────────────────────────────────────────────
Gateway refunds ("send money back to the customer") for payments that were
collected through a payment link.

* Bookings → delegated to the existing :class:`RefundService` (keeps the booking
  payment_status, SQS ``booking.refund_completed`` event and accounting flow).
* Gift vouchers → handled here: gateway refund, Refund record, payment history
  and — on a full refund of an unredeemed voucher — voucher cancellation.

The gateway call is made through the provider-agnostic
:class:`PaymentRefunder` interface; no provider is referenced here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.payment.application.refund_service import RefundService
from app.payment.domain.gateway_protocol import RefundRequest
from app.payment.domain.value_objects import (
    PaymentTransactionStatus,
    RefundMethod,
    RefundStatus,
    RefundType,
)
from app.payment.infrastructure.models import Payment, PaymentStatusHistory, Refund
from app.payment.infrastructure.repository import generate_refund_number
from app.payment.interfaces.schemas import ProcessGatewayRefundRequest
from app.payment_links.domain.gateway import PaymentRefunder
from app.payment_links.infrastructure.models import (
    PaymentLink,
    PaymentLinkStatus,
    PaymentLinkTarget,
)
from app.voucher.application.voucher_service import GiftVoucherService
from app.voucher.domain.value_objects import GiftVoucherStatus
from app.voucher.infrastructure.models import GiftVoucher

logger = get_logger(__name__)

_REFUNDABLE = (
    PaymentTransactionStatus.SUCCESS.value,
    PaymentTransactionStatus.PARTIALLY_REFUNDED.value,
)
_REFUNDABLE_LINK = (
    PaymentLinkStatus.PAID.value,
    PaymentLinkStatus.PARTIALLY_REFUNDED.value,
)


class PaymentLinkRefundService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        gateway: PaymentRefunder,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._session = session
        self._settings = settings
        self._gateway = gateway
        self._http = http_client

    async def refund(
        self,
        link_id: uuid.UUID,
        *,
        amount: Decimal | None,
        reason: str | None,
        actor_id: str,
        actor_data: dict[str, Any] | None = None,
    ) -> Refund:
        link = (
            await self._session.execute(select(PaymentLink).where(PaymentLink.id == link_id).with_for_update())
        ).scalar_one_or_none()
        if link is None:
            raise NotFoundError("Payment link not found.")
        if link.status not in _REFUNDABLE_LINK:
            raise ValidationError(f"Only paid payment links can be refunded (current status: {link.status}).")

        payment = await self._find_payment(link)
        if payment is None:
            raise ValidationError(
                "The payment record for this link has not been created yet; please retry shortly."
            )
        if payment.status not in _REFUNDABLE:
            raise ValidationError(f"Only paid payments can be refunded (current status: {payment.status}).")

        if link.target_type == PaymentLinkTarget.BOOKING.value:
            delegate = RefundService(self._session, self._settings, self._http)
            refund = await delegate.process_gateway_refund(
                link.target_id,
                ProcessGatewayRefundRequest(refund_amount=amount, reason=reason),
                actor_id,
                actor_data,
            )
        elif link.target_type == PaymentLinkTarget.GIFT_VOUCHER.value:
            refund = await self._refund_voucher_payment(payment, amount, reason, actor_id, actor_data)
        else:
            raise ValidationError("Refunds are not supported for this payment link target.")

        self._sync_link_refund(link, refund)
        return refund

    async def _find_payment(self, link: PaymentLink) -> Payment | None:
        """The ``payments`` row created after the payment succeeded (success-first, newest-first)."""
        stmt = select(Payment)
        if link.payment_row_id:
            stmt = stmt.where(Payment.id == link.payment_row_id)
        elif link.target_type == PaymentLinkTarget.BOOKING.value:
            stmt = stmt.where(Payment.booking_id == link.target_id)
        else:
            stmt = stmt.where(Payment.voucher_id == link.target_id)
        stmt = stmt.order_by(
            (Payment.status.in_(_REFUNDABLE)).desc(), Payment.created_at.desc()
        ).limit(1).with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _sync_link_refund(link: PaymentLink, refund: Refund) -> None:
        refunded = Decimal(str(link.amount_refunded or 0)) + Decimal(str(refund.refunded_amount or 0))
        link.amount_refunded = refunded
        link.status = (
            PaymentLinkStatus.REFUNDED.value
            if refunded >= Decimal(str(link.amount))
            else PaymentLinkStatus.PARTIALLY_REFUNDED.value
        )

    async def _refund_voucher_payment(
        self,
        payment: Payment,
        amount: Decimal | None,
        reason: str | None,
        actor_id: str,
        actor_data: dict[str, Any] | None,
    ) -> Refund:
        voucher = (
            await self._session.execute(select(GiftVoucher).where(GiftVoucher.id == payment.voucher_id).with_for_update())
        ).scalar_one_or_none()
        if voucher is not None and voucher.status in (
            GiftVoucherStatus.REDEEMED.value,
            GiftVoucherStatus.FULFILLED.value,
        ):
            raise ValidationError("A redeemed gift voucher cannot be refunded.")

        total = Decimal(str(payment.total_amount))
        already = Decimal(str(payment.amount_refunded or 0))
        remaining = total - already
        amount = amount if amount is not None else remaining
        if amount <= 0 or amount > remaining:
            raise ValidationError(f"Refund amount must be between 0 and {remaining}.")
        gateway_ref = payment.invoice_id or payment.payment_id
        if not gateway_ref:
            raise ValidationError("Payment lacks a gateway reference required for refund.")

        refund_number = await generate_refund_number(self._session)
        base = dict(
            refund_number=refund_number,
            payment_id=payment.id,
            invoice_number=payment.invoice_number,
            customer_id=payment.customer_id,
            customer_data=payment.customer_data,
            branch_id=payment.branch_id,
            branch_data=payment.branch_data,
            refund_type=RefundType.AUTOMATED.value,
            refund_method=RefundMethod.PAYMENT_LINK.value,
            requested_amount=amount,
            cancellation_fee=Decimal("0.000"),
            refunded_amount=amount,
            currency=payment.currency,
            reason=reason or "Gift voucher refund",
            payment_gateway=payment.payment_provider,
            gateway_transaction_id=payment.transaction_id,
            processed_by=actor_id,
            processed_by_data=actor_data,
            processed_at=datetime.now(timezone.utc),
        )

        try:
            result = await self._gateway.refund_payment(
                RefundRequest(provider_payment_id=gateway_ref, amount=amount, reason=reason or "Gift voucher refund")
            )
        except Exception as exc:
            self._session.add(Refund(**base, status=RefundStatus.FAILED.value, notes=f"Gateway error: {exc}"))
            await self._session.flush()
            raise

        refund = Refund(
            **base,
            status=RefundStatus.COMPLETED.value,
            gateway_refund_id=result.provider_refund_id,
            gateway_response=result.raw_response,
        )
        self._session.add(refund)

        old = payment.status
        payment.amount_refunded = already + amount
        payment.status = (
            PaymentTransactionStatus.REFUNDED.value
            if payment.amount_refunded >= total
            else PaymentTransactionStatus.PARTIALLY_REFUNDED.value
        )
        self._session.add(
            PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old,
                new_status=payment.status,
                source="ushdesk",
                reason=f"Gateway refund {refund_number}",
                provider_reference=result.provider_refund_id,
                correlation_id=refund_number,
                change_by_user=actor_id,
                change_by_user_data=actor_data,
            )
        )
        await self._session.flush()

        if (
            voucher is not None
            and payment.status == PaymentTransactionStatus.REFUNDED.value
            and voucher.status == GiftVoucherStatus.ACTIVE.value
        ):
            await GiftVoucherService(self._session).update_status(
                voucher.id, GiftVoucherStatus.CANCELLED.value, actor_id=actor_id
            )
        logger.info("payment_link_refund_completed", refund_number=refund_number, payment_id=str(payment.id))
        return refund
