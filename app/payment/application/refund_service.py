"""
app/payment/application/refund_service.py
─────────────────────────────────────────
Application service managing both Automated (Payment Gateway) and Manual (Desk / Branch) refunds.

Guarantees accounting and financial immutability:
- Original booking, invoice, and payment records remain intact for audit.
- Generates sequential refund numbers (REF/YYYY/MM/{NNNNNN}).
- Creates auditable Refund records.
- Emits SQS event `booking.refund_completed` for downstream orchestration (ushanr, ushnotice).
"""

from __future__ import annotations

from app.common.utils import to_local_tz as _to_local_tz
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.contracts import BookingRefundCompletedEvent
from app.booking.infrastructure.models import Booking
from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.events.sqs_client import get_sqs_client
from app.payment.domain.gateway_protocol import RefundRequest
from app.payment.domain.value_objects import (
    PaymentTransactionStatus,
    RefundMethod,
    RefundStatus,
    RefundType,
)
from app.payment.infrastructure.models import Payment, PaymentStatusHistory, Refund
from app.payment.infrastructure.providers.myfatoorah_provider import MyFatoorahProvider
from app.payment.infrastructure.providers.tap_provider import TapProvider
from app.payment.infrastructure.repository import generate_refund_number
from app.payment.interfaces.schemas import (
    CreateManualRefundRequest,
    ProcessGatewayRefundRequest,
)

logger = get_logger(__name__)

_KUWAIT_TZ = ZoneInfo("Asia/Kuwait")


def _now_kuwait() -> datetime:
    return datetime.now(timezone.utc)


class RefundService:
    """Manages refund workflows across automated payment gateways and manual desk operations."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def _get_booking(self, booking_id: uuid.UUID) -> Booking:
        stmt = select(Booking).where(Booking.id == booking_id)
        result = await self._session.execute(stmt)
        booking = result.scalar_one_or_none()
        if not booking:
            raise NotFoundError(f"Booking {booking_id} not found.")
        return booking

    async def _get_payment_for_booking(self, booking: Booking) -> Payment | None:
        stmt = select(Payment).where(Payment.booking_id == booking.id)
        result = await self._session.execute(stmt)
        payment = result.scalar_one_or_none()
        if payment:
            return payment

        # Fallback to booking.payment_id
        if getattr(booking, "payment_id", None):
            try:
                p_uuid = uuid.UUID(str(booking.payment_id))
                stmt = select(Payment).where(Payment.id == p_uuid)
                result = await self._session.execute(stmt)
                payment = result.scalar_one_or_none()
                if payment:
                    return payment
            except (ValueError, TypeError):
                stmt = select(Payment).where(Payment.payment_id == str(booking.payment_id))
                result = await self._session.execute(stmt)
                payment = result.scalar_one_or_none()
                if payment:
                    return payment

        return None

    def _resolve_gateway_provider(self, payment: Payment):
        prov = str(payment.payment_provider or payment.payment_gateway or "").strip().lower()
        if "fatoorah" in prov:
            return MyFatoorahProvider(http_client=self._http, settings=self._settings)
        return TapProvider(http_client=self._http, settings=self._settings)

    async def record_manual_refund(
        self,
        booking_id: uuid.UUID,
        req: CreateManualRefundRequest,
        current_user_id: str,
        current_user_data: dict[str, Any] | None = None,
    ) -> Refund:
        """
        Record a staff-assisted manual refund executed outside the automated payment gateway
        (e.g. cash, card at branch, or bank transfer).
        """
        booking = await self._get_booking(booking_id)
        payment = await self._get_payment_for_booking(booking)

        requested_amount = Decimal(str(req.refund_amount))
        cancellation_fee = Decimal(str(req.cancellation_fee or "0.000"))
        if requested_amount <= 0:
            raise ValidationError("Refund amount must be greater than zero.")
        if cancellation_fee < 0:
            raise ValidationError("Cancellation fee cannot be negative.")
        if cancellation_fee > requested_amount:
            raise ValidationError("Cancellation fee cannot exceed requested refund amount.")

        refunded_amount = requested_amount - cancellation_fee

        # If payment exists, validate that we don't exceed remaining payment total
        if payment:
            already_refunded = Decimal(str(payment.amount_refunded or "0.000"))
            total_amt = Decimal(str(payment.total_amount))
            if (already_refunded + refunded_amount) > total_amt:
                raise ValidationError(
                    f"Refund amount {refunded_amount} exceeds available refundable balance "
                    f"({total_amt - already_refunded} KWD)."
                )

        # Generate unique sequential refund number (e.g. REF/2026/10/000001)
        refund_number = await generate_refund_number(self._session)

        # Process user info
        effective_user_id = req.change_by_user or current_user_id
        effective_user_data = req.change_by_user_data or current_user_data or {}

        # Create Refund record
        refund = Refund(
            refund_number=refund_number,
            booking_id=booking.id,
            booking_data={
                "booking_number": getattr(booking, "booking_number", None),
                "status": booking.status,
                "appointment_start": _to_local_tz(booking.appointment_start).isoformat() if getattr(booking, "appointment_start", None) else None,
                "service_id": str(booking.service_id) if booking.service_id else None,
                "branch_id": str(booking.branch_id) if booking.branch_id else None,
            },
            payment_id=payment.id if payment else None,
            invoice_number=getattr(payment, "invoice_number", None) or getattr(booking, "invoice_number", None),
            customer_id=booking.customer_id,
            customer_data=booking.customer_data,
            branch_id=booking.branch_id,
            branch_data=booking.branch_data,
            refund_type=RefundType.MANUAL.value,
            refund_method=RefundMethod.normalise(req.refund_method).value,
            status=RefundStatus.COMPLETED.value,
            requested_amount=requested_amount,
            cancellation_fee=cancellation_fee,
            refunded_amount=refunded_amount,
            currency=booking.currency or (payment.currency if payment else "KWD"),
            reason=req.reason or "Manual staff refund",
            notes=req.notes,
            customer_confirmation=req.customer_confirmation,
            reference_number=req.reference_number,
            payment_gateway=None,
            gateway_transaction_id=None,
            gateway_refund_id=None,
            processed_by=str(effective_user_id),
            processed_by_data=effective_user_data,
            processed_at=_now_kuwait(),
        )

        self._session.add(refund)

        # Update payment refund counter and record status history (without destroying payment!)
        if payment:
            old_payment_status = payment.status
            payment.amount_refunded = Decimal(str(payment.amount_refunded or "0.000")) + refunded_amount
            if payment.amount_refunded >= Decimal(str(payment.total_amount)):
                payment.status = PaymentTransactionStatus.REFUNDED.value
            else:
                payment.status = PaymentTransactionStatus.PARTIALLY_REFUNDED.value

            history = PaymentStatusHistory(
                payment_id=payment.id,
                old_status=old_payment_status,
                new_status=payment.status,
                source="ushdesk",
                reason=f"Manual refund recorded: {refund_number} ({req.refund_method})",
                change_by_user=str(effective_user_id),
                change_by_user_data=effective_user_data,
                correlation_id=refund_number,
            )
            self._session.add(history)

        # Update booking payment_status
        if str(booking.payment_status).lower() in ("paid", "success"):
            booking.payment_status = "refunded" if (not payment or payment.status == PaymentTransactionStatus.REFUNDED.value) else "partially_refunded"
        if req.notes:
            booking.internal_notes = (f"{booking.internal_notes or ''}\n[Refund {refund_number}]: {req.notes}").strip()

        await self._session.flush()

        # Emit BookingRefundCompletedEvent to SQS for downstream services
        await self._publish_refund_event(booking, refund, payment)

        logger.info(
            "manual_refund_recorded",
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            booking_id=str(booking.id),
            amount=str(refunded_amount),
            method=refund.refund_method,
        )
        return refund

    async def process_gateway_refund(
        self,
        booking_id: uuid.UUID,
        req: ProcessGatewayRefundRequest,
        current_user_id: str,
        current_user_data: dict[str, Any] | None = None,
    ) -> Refund:
        """
        Process an automated refund via payment gateway (Tap or MyFatoorah).
        Only marks refund as COMPLETED once the gateway approves the refund.
        """
        booking = await self._get_booking(booking_id)
        payment = await self._get_payment_for_booking(booking)
        if not payment:
            raise ValidationError("No payment record found for this booking to process a gateway refund.")

        already_refunded = Decimal(str(payment.amount_refunded or "0.000"))
        remaining_balance = Decimal(str(payment.total_amount)) - already_refunded
        if remaining_balance <= 0:
            raise ValidationError("Payment has already been fully refunded.")

        requested_amount = Decimal(str(req.refund_amount)) if req.refund_amount is not None else remaining_balance
        cancellation_fee = Decimal(str(req.cancellation_fee or "0.000"))

        if requested_amount <= 0:
            raise ValidationError("Refund amount must be greater than zero.")
        if requested_amount > remaining_balance:
            raise ValidationError(
                f"Requested refund amount {requested_amount} exceeds remaining balance {remaining_balance}."
            )
        if cancellation_fee < 0 or cancellation_fee > requested_amount:
            raise ValidationError("Invalid cancellation fee.")

        refunded_amount = requested_amount - cancellation_fee
        if refunded_amount <= 0:
            raise ValidationError("Net refunded amount after cancellation fee must be greater than zero.")

        # Identify external gateway transaction id
        gateway_payment_id = payment.payment_id or payment.transaction_id or payment.invoice_id
        if not gateway_payment_id:
            raise ValidationError("Payment lacks external gateway reference required for automated refund.")

        effective_user_id = req.change_by_user or current_user_id
        effective_user_data = req.change_by_user_data or current_user_data or {}

        # Execute refund with gateway provider
        provider = self._resolve_gateway_provider(payment)
        gateway_req = RefundRequest(
            provider_payment_id=gateway_payment_id,
            amount=refunded_amount,
            reason=req.reason or "Booking cancellation refund",
        )

        refund_number = await generate_refund_number(self._session)

        try:
            gateway_res = await provider.refund_payment(gateway_req)
        except Exception as exc:
            logger.error(
                "gateway_refund_failed_exception",
                booking_id=str(booking.id),
                payment_id=str(payment.id),
                error=str(exc),
            )
            # Record failed refund record for audit
            failed_refund = Refund(
                refund_number=refund_number,
                booking_id=booking.id,
                payment_id=payment.id,
                customer_id=booking.customer_id,
                branch_id=booking.branch_id,
                refund_type=RefundType.AUTOMATED.value,
                refund_method=RefundMethod.PAYMENT_GATEWAY.value,
                status=RefundStatus.FAILED.value,
                requested_amount=requested_amount,
                cancellation_fee=cancellation_fee,
                refunded_amount=refunded_amount,
                currency=payment.currency,
                reason=req.reason,
                notes=f"Gateway execution failed: {exc}",
                payment_gateway=payment.payment_gateway or payment.payment_provider,
                gateway_transaction_id=payment.transaction_id,
                processed_by=str(effective_user_id),
                processed_by_data=effective_user_data,
                processed_at=_now_kuwait(),
            )
            self._session.add(failed_refund)
            await self._session.flush()
            raise ValidationError(f"Payment gateway refund failed: {exc}")

        if not gateway_res.is_successful:
            logger.warning(
                "gateway_refund_rejected",
                booking_id=str(booking.id),
                payment_id=str(payment.id),
                reason=gateway_res.failure_reason,
            )
            failed_refund = Refund(
                refund_number=refund_number,
                booking_id=booking.id,
                payment_id=payment.id,
                customer_id=booking.customer_id,
                branch_id=booking.branch_id,
                refund_type=RefundType.AUTOMATED.value,
                refund_method=RefundMethod.PAYMENT_GATEWAY.value,
                status=RefundStatus.FAILED.value,
                requested_amount=requested_amount,
                cancellation_fee=cancellation_fee,
                refunded_amount=refunded_amount,
                currency=payment.currency,
                reason=req.reason,
                notes=f"Gateway rejected: {gateway_res.failure_reason}",
                payment_gateway=payment.payment_gateway or payment.payment_provider,
                gateway_transaction_id=payment.transaction_id,
                gateway_response=gateway_res.raw_response,
                processed_by=str(effective_user_id),
                processed_by_data=effective_user_data,
                processed_at=_now_kuwait(),
            )
            self._session.add(failed_refund)
            await self._session.flush()
            raise ValidationError(f"Gateway refund declined: {gateway_res.failure_reason}")

        # Gateway Succeeded: Create completed Refund record
        refund = Refund(
            refund_number=refund_number,
            booking_id=booking.id,
            booking_data={
                "booking_number": getattr(booking, "booking_number", None),
                "status": booking.status,
                "appointment_start": _to_local_tz(booking.appointment_start).isoformat() if getattr(booking, "appointment_start", None) else None,
                "service_id": str(booking.service_id) if booking.service_id else None,
                "branch_id": str(booking.branch_id) if booking.branch_id else None,
            },
            payment_id=payment.id,
            invoice_number=payment.invoice_number or getattr(booking, "invoice_number", None),
            customer_id=booking.customer_id,
            customer_data=booking.customer_data,
            branch_id=booking.branch_id,
            branch_data=booking.branch_data,
            refund_type=RefundType.AUTOMATED.value,
            refund_method=RefundMethod.PAYMENT_GATEWAY.value,
            status=RefundStatus.COMPLETED.value,
            requested_amount=requested_amount,
            cancellation_fee=cancellation_fee,
            refunded_amount=refunded_amount,
            currency=payment.currency,
            reason=req.reason or "Automated gateway refund",
            notes=req.notes,
            payment_gateway=payment.payment_gateway or payment.payment_provider,
            gateway_transaction_id=payment.transaction_id,
            gateway_refund_id=gateway_res.provider_refund_id,
            gateway_response=gateway_res.raw_response,
            processed_by=str(effective_user_id),
            processed_by_data=effective_user_data,
            processed_at=_now_kuwait(),
        )
        self._session.add(refund)

        # Update payment record
        old_payment_status = payment.status
        payment.amount_refunded = Decimal(str(payment.amount_refunded or "0.000")) + refunded_amount
        if payment.amount_refunded >= Decimal(str(payment.total_amount)):
            payment.status = PaymentTransactionStatus.REFUNDED.value
        else:
            payment.status = PaymentTransactionStatus.PARTIALLY_REFUNDED.value

        history = PaymentStatusHistory(
            payment_id=payment.id,
            old_status=old_payment_status,
            new_status=payment.status,
            source="gateway_refund",
            reason=f"Gateway refund processed: {refund_number} (Gateway Refund ID: {gateway_res.provider_refund_id})",
            provider_reference=gateway_res.provider_refund_id,
            change_by_user=str(effective_user_id),
            change_by_user_data=effective_user_data,
            correlation_id=refund_number,
        )
        self._session.add(history)

        # Update booking payment_status
        booking.payment_status = "refunded" if payment.status == PaymentTransactionStatus.REFUNDED.value else "partially_refunded"

        await self._session.flush()

        # Emit SQS event for orchestration
        await self._publish_refund_event(booking, refund, payment)

        logger.info(
            "gateway_refund_completed",
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            gateway_refund_id=refund.gateway_refund_id,
            amount=str(refunded_amount),
        )
        return refund

    async def _publish_refund_event(
        self, booking: Booking, refund: Refund, payment: Payment | None
    ) -> None:
        c_dict = booking.customer_data or {}
        b_dict = booking.branch_data or {}
        s_dict = booking.service_data or {}

        c_name = str(
            c_dict.get("name")
            or c_dict.get("full_name")
            or f"{c_dict.get('first_name', '')} {c_dict.get('last_name', '')}".strip()
            or ""
        )
        c_phone = str(c_dict.get("phone_number") or c_dict.get("phone") or "")
        c_email = str(c_dict.get("email") or "")

        event = BookingRefundCompletedEvent(
            refund_id=str(refund.id),
            refund_number=refund.refund_number,
            booking_id=str(booking.id),
            booking_number=getattr(booking, "booking_number", None) or "",
            booking_reference=str(getattr(booking, "booking_number", None) or booking.id),
            payment_id=str(payment.id) if payment else "",
            customer_id=str(booking.customer_id) if booking.customer_id else "",
            customer_name=c_name,
            customer_phone=c_phone,
            customer_email=c_email,
            customer_language=str(c_dict.get("language_preference") or "en"),
            branch_id=str(booking.branch_id) if booking.branch_id else "",
            branch_name=str(b_dict.get("name") or ""),
            service_id=str(booking.service_id) if booking.service_id else "",
            service_name=str(s_dict.get("name") or ""),
            refund_type=refund.refund_type,
            refund_method=refund.refund_method,
            status=refund.status,
            requested_amount=str(refund.requested_amount),
            cancellation_fee=str(refund.cancellation_fee),
            refund_amount=str(refund.refunded_amount),
            currency=refund.currency,
            reason=refund.reason or "",
            notes=refund.notes or "",
            customer_confirmation=refund.customer_confirmation or "",
            reference_number=refund.reference_number or "",
            payment_gateway=refund.payment_gateway,
            gateway_refund_id=refund.gateway_refund_id,
            gateway_transaction_id=refund.gateway_transaction_id,
            processed_by=refund.processed_by or "",
            processed_by_data=refund.processed_by_data or {},
            processed_at=_to_local_tz(refund.processed_at).isoformat() if refund.processed_at else "",
            created_at=_to_local_tz(refund.created_at).isoformat() if refund.created_at else "",
        )

        try:
            sqs = get_sqs_client()
            import asyncio
            asyncio.create_task(sqs.publish_event(event))
            logger.info("refund_event_published", refund_number=refund.refund_number)
        except Exception as exc:
            logger.warning("refund_event_publish_failed", refund_number=refund.refund_number, error=str(exc))

    async def get_booking_refunds(self, booking_id: uuid.UUID) -> list[Refund]:
        stmt = select(Refund).where(Refund.booking_id == booking_id).order_by(Refund.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_refund_by_id(self, refund_id: uuid.UUID) -> Refund:
        stmt = select(Refund).where(Refund.id == refund_id)
        result = await self._session.execute(stmt)
        refund = result.scalar_one_or_none()
        if not refund:
            raise NotFoundError(f"Refund {refund_id} not found.")
        return refund

    async def list_refunds(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        booking_id: uuid.UUID | str | None = None,
        payment_id: uuid.UUID | str | None = None,
        customer_id: uuid.UUID | str | None = None,
        branch_id: uuid.UUID | str | None = None,
        refund_type: str | None = None,
        refund_method: str | None = None,
        status: str | None = None,
        refund_number: str | None = None,
        invoice_number: str | None = None,
        credit_note_number: str | None = None,
        payment_gateway: str | None = None,
        gateway_refund_id: str | None = None,
        gateway_transaction_id: str | None = None,
        reference_number: str | None = None,
        processed_by: str | None = None,
        from_date: datetime | str | None = None,
        to_date: datetime | str | None = None,
        search: str | None = None,
        currency: str | None = None,
    ) -> tuple[list[Refund], int, dict[str, Any]]:
        """
        List all refunds with pagination, multi-attribute filtering, and aggregate analytics.
        """
        conditions = []

        if booking_id:
            try:
                b_uuid = uuid.UUID(str(booking_id))
                conditions.append(Refund.booking_id == b_uuid)
            except (ValueError, TypeError):
                pass

        if payment_id:
            try:
                p_uuid = uuid.UUID(str(payment_id))
                conditions.append(Refund.payment_id == p_uuid)
            except (ValueError, TypeError):
                pass

        if customer_id:
            try:
                c_uuid = uuid.UUID(str(customer_id))
                conditions.append(Refund.customer_id == c_uuid)
            except (ValueError, TypeError):
                pass

        if branch_id:
            try:
                br_uuid = uuid.UUID(str(branch_id))
                conditions.append(Refund.branch_id == br_uuid)
            except (ValueError, TypeError):
                pass

        if refund_type and isinstance(refund_type, str):
            conditions.append(Refund.refund_type == refund_type.lower().strip())

        if refund_method and isinstance(refund_method, str):
            conditions.append(Refund.refund_method == refund_method.lower().strip())

        if status and isinstance(status, str):
            conditions.append(Refund.status == status.lower().strip())

        if refund_number and isinstance(refund_number, str):
            conditions.append(Refund.refund_number.ilike(f"%{refund_number.strip()}%"))

        if invoice_number and isinstance(invoice_number, str):
            conditions.append(Refund.invoice_number.ilike(f"%{invoice_number.strip()}%"))

        if credit_note_number and isinstance(credit_note_number, str):
            conditions.append(Refund.credit_note_number.ilike(f"%{credit_note_number.strip()}%"))

        if payment_gateway and isinstance(payment_gateway, str):
            conditions.append(Refund.payment_gateway.ilike(f"%{payment_gateway.strip()}%"))

        if gateway_refund_id and isinstance(gateway_refund_id, str):
            conditions.append(Refund.gateway_refund_id == gateway_refund_id.strip())

        if gateway_transaction_id and isinstance(gateway_transaction_id, str):
            conditions.append(Refund.gateway_transaction_id == gateway_transaction_id.strip())

        if reference_number and isinstance(reference_number, str):
            conditions.append(Refund.reference_number.ilike(f"%{reference_number.strip()}%"))

        if processed_by and isinstance(processed_by, str):
            conditions.append(Refund.processed_by == processed_by.strip())

        if currency and isinstance(currency, str):
            conditions.append(Refund.currency == currency.upper().strip())

        if from_date:
            try:
                dt_from = from_date if isinstance(from_date, datetime) else datetime.fromisoformat(str(from_date))
                conditions.append(Refund.created_at >= dt_from)
            except Exception:
                pass

        if to_date:
            try:
                dt_to = to_date if isinstance(to_date, datetime) else datetime.fromisoformat(str(to_date))
                conditions.append(Refund.created_at <= dt_to)
            except Exception:
                pass

        if search and isinstance(search, str):
            s = f"%{search.strip()}%"
            conditions.append(
                or_(
                    Refund.refund_number.ilike(s),
                    Refund.invoice_number.ilike(s),
                    Refund.credit_note_number.ilike(s),
                    Refund.reference_number.ilike(s),
                    Refund.gateway_refund_id.ilike(s),
                    Refund.gateway_transaction_id.ilike(s),
                    Refund.reason.ilike(s),
                    Refund.notes.ilike(s),
                    Refund.processed_by.ilike(s),
                )
            )

        # Count total
        count_stmt = select(func.count(Refund.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_count = (await self._session.execute(count_stmt)).scalar_one()

        # Aggregates
        sum_stmt = select(
            func.coalesce(func.sum(Refund.refunded_amount), Decimal("0.000")).label("total_refunded_amount"),
            func.coalesce(func.sum(Refund.cancellation_fee), Decimal("0.000")).label("total_cancellation_fee"),
            func.coalesce(func.sum(Refund.requested_amount), Decimal("0.000")).label("total_requested_amount"),
        )
        if conditions:
            sum_stmt = sum_stmt.where(and_(*conditions))
        agg_res = (await self._session.execute(sum_stmt)).one()

        analytics = {
            "total_refunded_amount": str(agg_res.total_refunded_amount),
            "total_cancellation_fee": str(agg_res.total_cancellation_fee),
            "total_requested_amount": str(agg_res.total_requested_amount),
            "total_count": total_count,
        }

        # Data query
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_page_size

        data_stmt = (
            select(Refund)
            .order_by(Refund.created_at.desc())
            .offset(offset)
            .limit(safe_page_size)
        )
        if conditions:
            data_stmt = data_stmt.where(and_(*conditions))

        result = await self._session.execute(data_stmt)
        refunds = list(result.scalars().all())

        return refunds, total_count, analytics
