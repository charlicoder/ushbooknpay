"""
app/course/application/services.py
──────────────────────────────────
CourseSubscriptionService — use cases of the course-subscription domain.

Every state change runs inside the caller's DB transaction (the service never
commits) under a row lock on the subscription (``SELECT … FOR UPDATE``), and
writes its event to the transactional outbox in the same transaction.

Use cases
---------
create_subscription      purchase (payment_pending) — price computed on the server
create_payment_link      (re)issue a hosted payment link
activate                 payment succeeded → Payment row, active, invoice (idempotent)
mark_payment_failed      payment failed → payment_failed
cancel                   only before payment
refund                   full refund, only when nothing used / booked (rule A5)
lock_for_booking         lock + validate for a redemption booking (contract E)
reserve                  create the usage row (reserved_sessions += 1)
consume / release        booking completed|no_show / cancelled|refunded (idempotent)
reactivate               booking moved back to an active status after a release
refresh_usage_snapshot   reschedule / edit of the booking
expire_due               hourly expiry task
retry_failed_invoices    ushanr retry task
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from unittest.mock import Mock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.common.utils import to_local_tz
from app.core.config import Settings, get_settings
from app.core.exceptions import GatewayTimeoutError, NotFoundError
from app.core.logging import get_logger
from app.course.domain import rules
from app.course.domain.value_objects import (
    COURSE_CANCEL_NOT_ALLOWED,
    COURSE_NOT_AVAILABLE,
    COURSE_NOT_FOUND,
    COURSE_PAYMENT_LINK_NOT_ALLOWED,
    COURSE_REFUND_NOT_ALLOWED,
    COURSE_SUBSCRIPTION_NOT_FOUND,
    COURSE_SUBSCRIPTION_NOT_PAYABLE,
    PAID_STATUSES,
    PAYABLE_STATUSES,
    CourseError,
    CourseInvoiceStatus,
    CoursePaymentOption,
    CourseSubscriptionStatus,
    CourseUsageStatus,
)
from app.course.infrastructure.models import CourseSessionUsage, CourseSubscription
from app.course.infrastructure.repository import CourseSubscriptionRepository
from app.events.contracts import (
    CourseSubscriptionActivatedEvent,
    CourseSubscriptionCreatedEvent,
    CourseSubscriptionExpiredEvent,
    CourseSubscriptionInvoiceCreatedEvent,
    CourseSubscriptionPaymentFailedEvent,
    CourseSubscriptionRefundedEvent,
    CourseSubscriptionSessionConsumedEvent,
    CourseSubscriptionSessionReleasedEvent,
    CourseSubscriptionSessionReservedEvent,
)
from app.events.outbox.writer import add_outbox_event

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    local = to_local_tz(dt)
    return local.isoformat() if local else ""


def _uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


# ── Snapshots ─────────────────────────────────────────────────────────────────


def build_customer_data(customer_id: Any, raw_profile: dict[str, Any] | None) -> dict[str, Any]:
    """
    Flat customer snapshot from the ushauth customer profile (same shape as booking
    creation) plus ``name``, ``whatsapp_verified`` and ``language_preference`` for
    ushnotice.
    """
    profile = raw_profile or {}
    prof = profile.get("data", profile) if isinstance(profile.get("data", profile), dict) else {}
    user = prof.get("user") if isinstance(prof.get("user"), dict) else {}

    def pick(*keys: str) -> Any:
        for k in keys:
            for src in (prof, user):
                v = src.get(k)
                if v not in (None, ""):
                    return v
        return None

    first = str(pick("first_name") or "")
    last = str(pick("last_name") or "")
    lang_raw = str(pick("language_preference", "language") or "en")
    data: dict[str, Any] = {
        "id": str(customer_id),
        "name": f"{first} {last}".strip() or str(pick("name", "full_name") or ""),
        "first_name": first,
        "last_name": last,
        "phone_number": str(pick("phone_number", "mobile", "phone") or ""),
        "email": str(pick("email") or ""),
        "language_preference": "ar" if lang_raw.strip().lower().startswith("ar") else "en",
    }
    whatsapp = pick("whatsapp_verified", "is_whatsapp_verified")
    if whatsapp is not None:
        data["whatsapp_verified"] = bool(whatsapp)
    return data


def build_course_snapshot(course: dict[str, Any]) -> dict[str, Any]:
    """``course_data`` snapshot (contract D)."""
    service = course.get("service") if isinstance(course.get("service"), dict) else {}
    return {
        "id": str(course.get("id") or ""),
        "code": str(course.get("code") or ""),
        "name": str(course.get("name") or course.get("name_en") or ""),
        "name_en": str(course.get("name_en") or course.get("name") or ""),
        "name_ar": str(course.get("name_ar") or ""),
        "service_id": str(course.get("service_id") or service.get("id") or ""),
        "service_name": str(service.get("name") or course.get("service_name") or ""),
        "service_duration_minutes": int(service.get("duration_minutes") or course.get("service_duration_minutes") or 0),
        "arrangement_type": str(course.get("arrangement_type") or ""),
        "arrangement_type_label": str(course.get("arrangement_type_label") or ""),
        "unit_price": str(course.get("unit_price") or service.get("base_price") or "0.000"),
        "total_sessions": int(course.get("total_sessions") or 0),
        "validity_days": int(course.get("validity_days") or 0),
        "price": str(course.get("price") or "0.000"),
        "currency": str(course.get("currency") or "KWD"),
    }


def _sub_base_payload(sub: CourseSubscription) -> dict[str, Any]:
    return {
        "subscription_id": str(sub.id),
        "subscription_number": sub.subscription_number or "",
        "customer_id": str(sub.customer_id),
        "customer_data": dict(sub.customer_data or {}),
    }


def _purchase_payload(sub: CourseSubscription) -> dict[str, Any]:
    course = sub.course_data or {}
    return {
        **_sub_base_payload(sub),
        "course_id": str(sub.course_id),
        "course_code": str(course.get("code") or ""),
        "course_name": str(course.get("name") or ""),
        "service_name": str(course.get("service_name") or ""),
        "total_sessions": int(sub.total_sessions or 0),
        "price": f"{Decimal(str(sub.price or 0)):.3f}",
        "currency": sub.currency or "KWD",
        "payment_option": sub.payment_option or "",
        "payment_url": sub.payment_url or "",
    }


def _usage_payload(sub: CourseSubscription, usage: CourseSessionUsage) -> dict[str, Any]:
    return {
        **_sub_base_payload(sub),
        "course_name": str((sub.course_data or {}).get("name") or ""),
        "booking_id": str(usage.booking_id),
        "booking_number": usage.booking_number or "",
        "session_number": int(usage.session_number or 0),
        "appointment_start": _iso(usage.appointment_start),
        "available_sessions": rules.available_sessions(sub),
    }


def booking_course_data(sub: CourseSubscription, usage: CourseSessionUsage) -> dict[str, Any]:
    """``bookings.course_data`` snapshot (contract E)."""
    course = sub.course_data or {}
    total_sess = int(sub.total_sessions or 0)
    sess_num = int(usage.session_number or 1)
    remaining_sess = max(0, total_sess - sess_num)
    return {
        "subscription_id": str(sub.id),
        "subscription_number": sub.subscription_number,
        "course_id": str(sub.course_id),
        "course_code": str(course.get("code") or ""),
        "course_name": str(course.get("name") or ""),
        "session_number": sess_num,
        "total_sessions": total_sess,
        "remaining_sessions": remaining_sess,
        "price": f"{Decimal(str(sub.price or 0)):.3f}",
        "currency": sub.currency or "KWD",
        "payment_provider": sub.payment_provider,
        "payment_method": sub.payment_method,
        "payment_gateway": sub.payment_gateway,
        "payment_option": sub.payment_option,
        "payment_number": getattr(sub, "payment_number", None),
        "transaction_reference": (
            getattr(sub, "transaction_reference", None)
            or (sub.course_data or {}).get("transaction_reference")
        ),
        "invoice_number": sub.invoice_number,
        "invoice_id": str(sub.invoice_id) if sub.invoice_id else None,
        "purchased_at": _iso(sub.purchased_at),
        "activated_at": _iso(sub.activated_at),
    }


def _booking_snapshot(usage: CourseSessionUsage, booking: Any) -> None:
    """Copy the appointment snapshot of ``booking`` onto ``usage``."""
    from app.booking.domain.services_snapshot import entry_arrangement_name, get_booking_services, joined_service_name

    branch = booking.branch_data if isinstance(getattr(booking, "branch_data", None), dict) else {}
    therapist = booking.therapist_data if isinstance(getattr(booking, "therapist_data", None), dict) else {}
    try:
        entries = get_booking_services(booking)
    except Exception:
        entries = []
    usage.booking_number = getattr(booking, "booking_number", None) or usage.booking_number
    usage.branch_id = _uuid(getattr(booking, "branch_id", None))
    usage.branch_name = str(branch.get("name") or branch.get("branch_name") or "")[:255] or None
    usage.therapist_id = _uuid(getattr(booking, "therapist_id", None))
    usage.therapist_name = (
        str(therapist.get("name") or therapist.get("full_name") or therapist.get("therapist_name") or "")[:255] or None
    )
    usage.service_arrangement_id = _uuid(getattr(booking, "service_arrangement_id", None))
    usage.service_arrangement_name = (entry_arrangement_name(entries[0]) if entries else "")[:255] or None
    usage.service_name = (joined_service_name(entries) if entries else "")[:255] or None
    usage.appointment_start = getattr(booking, "appointment_start", None)
    usage.appointment_end = getattr(booking, "appointment_end", None)


class CourseSubscriptionService:
    def __init__(
        self,
        session: Any,
        settings: Settings | None = None,
        ushauth_client: Any = None,
        ushanr_factory: Any = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._repo = CourseSubscriptionRepository(session)
        self._ushauth = ushauth_client
        self._ushanr_factory = ushanr_factory

    # ── helpers ───────────────────────────────────────────────────────────
    def _get_ushauth(self) -> Any:
        if self._ushauth is None:
            from app.api.deps import get_http_client
            from app.common.redis_client import get_redis
            from app.integrations.ushauth_client import USHAuthClient

            self._ushauth = USHAuthClient(http_client=get_http_client(), redis_client=get_redis(), settings=self._settings)
        return self._ushauth

    def _new_ushanr(self) -> Any:
        if self._ushanr_factory is not None:
            return self._ushanr_factory()
        from app.integrations.ushanr_client import UshanrClient

        return UshanrClient()

    def _emit(self, event: Any, sub: CourseSubscription) -> None:
        add_outbox_event(self._session, event, correlation_id=str(sub.id))

    async def get(self, subscription_id: uuid.UUID, *, for_update: bool = False) -> CourseSubscription:
        sub = await self._repo.get(subscription_id, for_update=for_update)
        if sub is None:
            raise CourseError(COURSE_SUBSCRIPTION_NOT_FOUND, "Course subscription not found.")
        return sub

    # ── Purchase ──────────────────────────────────────────────────────────
    async def create_subscription(
        self,
        *,
        customer_id: uuid.UUID,
        course_id: uuid.UUID,
        payment_option: str = CoursePaymentOption.PAY_AT_DESK.value,
        payment_gateway: str | None = None,
        discount: Decimal | None = None,
        idempotency_key: str | None = None,
        notes: str | None = None,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
    ) -> tuple[CourseSubscription, bool]:
        """Returns ``(subscription, created)``; ``created=False`` for an idempotent replay."""
        if idempotency_key:
            existing = await self._repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing, False

        ushauth = self._get_ushauth()
        try:
            course = await ushauth.get_course(str(course_id))
        except NotFoundError:
            raise CourseError(COURSE_NOT_FOUND, "Course not found.")
        if str(course.get("status") or "").lower() != "active":
            raise CourseError(COURSE_NOT_AVAILABLE, "This course is not available for sale.")

        snapshot = build_course_snapshot(course)
        price = rules.compute_course_price(course)
        if discount is not None and discount > Decimal("0.000"):
            snapshot["original_price"] = str(price)
            snapshot["discount"] = str(discount)
            price = max(Decimal("0.000"), price - discount)
        if not snapshot["service_id"] or not snapshot["arrangement_type"] or snapshot["total_sessions"] <= 0:
            raise CourseError(COURSE_NOT_AVAILABLE, "The course definition is incomplete.")

        try:
            profile = await ushauth.get_customer_profile(str(customer_id))
        except NotFoundError:
            raise NotFoundError("Customer not found.")
        except GatewayTimeoutError:
            raise
        customer_data = build_customer_data(customer_id, profile)

        number = await self._repo.generate_subscription_number()
        sub = CourseSubscription(
            id=uuid.uuid4(),
            subscription_number=number,
            customer_id=customer_id,
            customer_data=customer_data,
            course_id=course_id,
            course_data=snapshot,
            service_id=uuid.UUID(snapshot["service_id"]),
            arrangement_type=snapshot["arrangement_type"],
            total_sessions=snapshot["total_sessions"],
            used_sessions=0,
            reserved_sessions=0,
            validity_days=snapshot["validity_days"] or 1,
            price=price,
            currency=snapshot["currency"] or "KWD",
            status=CourseSubscriptionStatus.PAYMENT_PENDING.value,
            purchased_at=_utcnow(),
            payment_option=payment_option,
            payment_gateway=payment_gateway,
            invoice_status=CourseInvoiceStatus.PENDING.value,
            invoice_attempts=0,
            sold_by_user=actor_id,
            sold_by_user_data=actor_data,
            notes=notes,
            idempotency_key=idempotency_key,
        )
        try:
            await self._insert_in_savepoint(sub)
        except IntegrityError:
            # Concurrent duplicate submit with the same idempotency key: return the winner.
            if idempotency_key:
                existing = await self._repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing, False
            raise

        if payment_option == CoursePaymentOption.PAYMENT_LINK.value:
            await self._issue_payment_link(sub, payment_gateway, actor_id or "system", actor_data)

        self._emit(CourseSubscriptionCreatedEvent(**_purchase_payload(sub)), sub)
        logger.info("course_subscription_created", subscription_id=str(sub.id), number=number, price=str(price))
        return sub, True

    async def _insert_in_savepoint(self, sub: CourseSubscription) -> None:
        begin_nested = getattr(self._session, "begin_nested", None)
        if begin_nested is not None and not isinstance(self._session, Mock):
            async with begin_nested():
                self._repo.add(sub)
                await self._repo.flush()
            return
        self._repo.add(sub)
        await self._repo.flush()

    async def _issue_payment_link(
        self,
        sub: CourseSubscription,
        payment_gateway: str | None,
        actor_id: str,
        actor_data: dict[str, Any] | None,
        *,
        force_new: bool = False,
    ) -> Any:
        from app.api.deps import get_http_client
        from app.payment_links import get_gateway
        from app.payment_links.application.link_service import PaymentLinkService

        gw_name = (payment_gateway or self._settings.PAYMENT_LINK_DEFAULT_GATEWAY or "myfatoorah").lower()
        gateway = get_gateway(gw_name, get_http_client(), self._settings)
        link = await PaymentLinkService(self._session, self._settings, gateway).create_link(
            target_type="course_subscription",  # type: ignore[arg-type]
            target_id=sub.id,
            amount=Decimal(str(sub.price)),
            actor_id=actor_id,
            actor_data=actor_data,
            force_new=force_new,
        )
        sub.payment_url = link.payment_url
        sub.payment_link_id = link.id
        sub.payment_gateway = link.gateway
        sub.payment_option = CoursePaymentOption.PAYMENT_LINK.value
        return link

    async def create_payment_link(
        self,
        subscription_id: uuid.UUID,
        *,
        payment_gateway: str | None = None,
        actor_id: str = "system",
        actor_data: dict[str, Any] | None = None,
    ) -> CourseSubscription:
        sub = await self.get(subscription_id, for_update=True)
        if sub.status not in PAYABLE_STATUSES:
            raise CourseError(
                COURSE_PAYMENT_LINK_NOT_ALLOWED,
                f"A payment link can only be created for unpaid subscriptions (status: {sub.status}).",
            )
        await self._issue_payment_link(sub, payment_gateway or sub.payment_gateway, actor_id, actor_data, force_new=True)
        await self._repo.flush()
        return sub

    # ── Activation ────────────────────────────────────────────────────────
    async def activate(
        self,
        subscription_id: uuid.UUID,
        *,
        payment_provider: str | None = None,
        payment_method: str | None = None,
        payment_gateway: str | None = None,
        transaction_reference: str | None = None,
        payment_through: str | None = "ushdesk",
        payment_link: Any = None,
        paid_at: datetime | None = None,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
        gateway_payment_data: dict[str, Any] | None = None,
    ) -> tuple[CourseSubscription, Any, bool]:
        """
        Payment succeeded → ``(subscription, payment, activated)``.

        Idempotent: once paid, further confirmations / webhooks return the existing
        payment with ``activated=False`` and change nothing.
        """
        from app.payment.infrastructure.models import Payment, PaymentStatusHistory
        from app.payment.infrastructure.repository import generate_payment_number

        sub = await self.get(subscription_id, for_update=True)
        existing_payment = await self._find_payment(sub)
        if sub.status in PAID_STATUSES or sub.status == CourseSubscriptionStatus.REFUNDED.value:
            logger.info("course_subscription_already_activated", subscription_id=str(sub.id))
            return sub, existing_payment, False
        if sub.status not in PAYABLE_STATUSES:
            raise CourseError(
                COURSE_SUBSCRIPTION_NOT_PAYABLE,
                f"The course subscription cannot be activated (status: {sub.status}).",
            )

        now = paid_at or _utcnow()
        payment = existing_payment
        if payment is None:
            course = sub.course_data or {}
            duration = int(course.get("service_duration_minutes") or 0) * int(sub.total_sessions or 0)
            payment = Payment(
                id=uuid.uuid4(),
                payment_number=await generate_payment_number(self._session),
                customer_id=sub.customer_id,
                customer_data=sub.customer_data,
                service_id=sub.service_id,
                service_data={
                    "id": str(sub.service_id),
                    "name": course.get("service_name") or "",
                    "course_id": str(sub.course_id),
                    "course_code": course.get("code") or "",
                    "course_name": course.get("name") or "",
                },
                total_amount=Decimal(str(sub.price)),
                total_duration=duration,
                currency=sub.currency or "KWD",
                status="success",
                paid_at=now,
                course_subscription_id=sub.id,
                payment_for="course_subscription",
                payment_provider=payment_provider,
                payment_method=payment_method,
                payment_gateway=payment_gateway,
                payment_through=payment_through or "ushdesk",
                payment_link_id=getattr(payment_link, "id", None),
                payment_url=getattr(payment_link, "payment_url", None),
                payment_id=(getattr(payment_link, "gateway_payment_id", None) or transaction_reference),
                transaction_id=getattr(payment_link, "transaction_id", None) or transaction_reference,
                track_id=getattr(payment_link, "track_id", None),
                reference_id=getattr(payment_link, "reference_id", None) or transaction_reference,
                transaction_status="SUCCESS",
                transaction_date=now.isoformat(),
                invoice_value=Decimal(str(sub.price)),
                payment_data={
                    **(gateway_payment_data or {}),
                    "course_subscription_id": str(sub.id),
                    "subscription_number": sub.subscription_number,
                    **({"payment_link_id": str(payment_link.id)} if payment_link is not None else {}),
                },
                created_by_user=actor_id,
                created_by_user_data=actor_data,
            )
            self._session.add(payment)
            await self._session.flush()
            self._session.add(
                PaymentStatusHistory(
                    payment_id=payment.id,
                    old_status=None,
                    new_status="success",
                    source="payment_link" if payment_link is not None else (payment_through or "ushdesk"),
                    reason=f"Course subscription {sub.subscription_number} paid",
                    provider_reference=transaction_reference,
                    correlation_id=str(sub.id),
                    change_by_user=actor_id,
                    change_by_user_data=actor_data,
                )
            )

        sub.status = CourseSubscriptionStatus.ACTIVE.value
        sub.activated_at = now
        sub.expires_at = rules.compute_expires_at(now, sub.validity_days)
        sub.payment_id = payment.id
        sub.payment_number = payment.payment_number
        sub.payment_provider = payment_provider or sub.payment_provider
        sub.payment_method = payment_method or sub.payment_method
        sub.payment_gateway = payment_gateway or sub.payment_gateway
        sub.payment_through = payment_through or sub.payment_through or "ushdesk"
        sub.transaction_reference = transaction_reference or sub.transaction_reference
        if payment_link is not None:
            sub.payment_link_id = payment_link.id
            sub.payment_url = payment_link.payment_url or sub.payment_url
        await self._repo.flush()

        await self._create_invoice(sub, payment)

        self._emit(
            CourseSubscriptionActivatedEvent(
                **_purchase_payload(sub),
                payment_number=sub.payment_number or "",
                invoice_number=sub.invoice_number or "",
                activated_at=_iso(sub.activated_at),
                expires_at=_iso(sub.expires_at),
            ),
            sub,
        )
        logger.info("course_subscription_activated", subscription_id=str(sub.id), payment_id=str(payment.id))
        return sub, payment, True

    async def _find_payment(self, sub: CourseSubscription) -> Any:
        from app.payment.infrastructure.models import Payment

        stmt = (
            select(Payment)
            .where(Payment.course_subscription_id == sub.id)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _create_invoice(self, sub: CourseSubscription, payment: Any = None) -> bool:
        """Create the ushanr invoice; on failure mark ``invoice_status=failed`` (retried later)."""
        if sub.invoice_status == CourseInvoiceStatus.CREATED.value and sub.invoice_number:
            return True
        sub.invoice_attempts = int(sub.invoice_attempts or 0) + 1
        client = self._new_ushanr()
        data = None
        error: str | None = None
        try:
            cust = sub.customer_data or {}
            data = await client.create_or_get_course_subscription_invoice_data(
                subscription=sub,
                customer_name=str(cust.get("name") or f"{cust.get('first_name', '')} {cust.get('last_name', '')}").strip(),
                customer_phone=str(cust.get("phone_number") or ""),
                customer_email=str(cust.get("email") or ""),
            )
        except Exception as exc:  # accounting must never break activation
            error = str(exc)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

        if not data or not data.get("invoice_number"):
            sub.invoice_status = CourseInvoiceStatus.FAILED.value
            sub.invoice_last_error = (error or "ushanr did not return an invoice")[:2000]
            await self._repo.flush()
            logger.warning("course_invoice_failed", subscription_id=str(sub.id), error=sub.invoice_last_error)
            return False

        sub.invoice_id = data.get("invoice_id") or None
        sub.invoice_number = data.get("invoice_number")
        sub.invoice_status = CourseInvoiceStatus.CREATED.value
        sub.invoice_last_error = None
        if payment is None and sub.payment_id:
            payment = await self._find_payment(sub)
        if payment is not None:
            payment.invoice_number = sub.invoice_number
            payment.invoice_id = sub.invoice_id
        await self._repo.flush()
        self._emit(
            CourseSubscriptionInvoiceCreatedEvent(
                subscription_id=str(sub.id),
                subscription_number=sub.subscription_number,
                invoice_id=sub.invoice_id or "",
                invoice_number=sub.invoice_number or "",
            ),
            sub,
        )
        return True

    # ── Payment failure / cancel / refund ─────────────────────────────────
    async def mark_payment_failed(self, subscription_id: uuid.UUID, *, reason: str = "") -> CourseSubscription:
        sub = await self.get(subscription_id, for_update=True)
        if sub.status == CourseSubscriptionStatus.PAYMENT_FAILED.value:
            return sub
        if sub.status != CourseSubscriptionStatus.PAYMENT_PENDING.value:
            raise CourseError(
                COURSE_SUBSCRIPTION_NOT_PAYABLE,
                f"Payment failure cannot be recorded (status: {sub.status}).",
            )
        sub.status = CourseSubscriptionStatus.PAYMENT_FAILED.value
        await self._repo.flush()
        self._emit(CourseSubscriptionPaymentFailedEvent(**_sub_base_payload(sub), reason=reason or ""), sub)
        return sub

    async def cancel(self, subscription_id: uuid.UUID, *, reason: str | None = None) -> CourseSubscription:
        sub = await self.get(subscription_id, for_update=True)
        if sub.status == CourseSubscriptionStatus.CANCELLED.value:
            return sub
        if sub.status not in PAYABLE_STATUSES:
            raise CourseError(
                COURSE_CANCEL_NOT_ALLOWED,
                f"Only unpaid subscriptions can be cancelled (status: {sub.status}).",
            )
        sub.status = CourseSubscriptionStatus.CANCELLED.value
        sub.cancelled_at = _utcnow()
        sub.cancel_reason = reason
        await self._cancel_open_links(sub)
        await self._repo.flush()
        return sub

    async def _cancel_open_links(self, sub: CourseSubscription) -> None:
        if not sub.payment_link_id:
            return
        try:
            from app.api.deps import get_http_client
            from app.payment_links import get_gateway
            from app.payment_links.application.link_service import PaymentLinkService

            gw = get_gateway(
                (sub.payment_gateway or self._settings.PAYMENT_LINK_DEFAULT_GATEWAY or "myfatoorah").lower(),
                get_http_client(),
                self._settings,
            )
            await PaymentLinkService(self._session, self._settings, gw).cancel_and_delete_links_for_target(
                "course_subscription", sub.id
            )
            sub.payment_url = None
        except Exception as exc:
            logger.warning("course_cancel_payment_links_failed", subscription_id=str(sub.id), error=str(exc))

    async def refund(
        self,
        subscription_id: uuid.UUID,
        *,
        reason: str,
        refund_method: str | None = None,
        actor_id: str | None = None,
        actor_data: dict[str, Any] | None = None,
    ) -> CourseSubscription:
        """Full refund (rule A5) + ushanr credit note. Idempotent once refunded."""
        from app.payment.infrastructure.models import PaymentStatusHistory, Refund
        from app.payment.infrastructure.repository import generate_refund_number

        sub = await self.get(subscription_id, for_update=True)
        if sub.status == CourseSubscriptionStatus.REFUNDED.value:
            return sub
        if not rules.can_refund(sub):
            raise CourseError(
                COURSE_REFUND_NOT_ALLOWED,
                "A course can only be refunded while no session is used or booked "
                "and the subscription is active or expired.",
                detail={
                    "status": sub.status,
                    "used_sessions": sub.used_sessions,
                    "reserved_sessions": sub.reserved_sessions,
                },
            )
        now = _utcnow()
        amount = Decimal(str(sub.price or 0))
        payment = await self._find_payment(sub)

        credit_note: dict[str, Any] | None = None
        client = self._new_ushanr()
        refund_number = await generate_refund_number(self._session)
        try:
            credit_note = await client.create_credit_note(
                source_document_type="course_subscription",
                source_document_id=str(sub.id),
                refund_amount=amount,
                cancellation_fee=Decimal("0.000"),
                refund_method=refund_method or "cash",
                refund_number=refund_number,
                processed_by=actor_id or "ushbooknpay",
                record_refund_payment=True,
                notes=f"Course subscription {sub.subscription_number} refunded: {reason}",
            )
        except Exception as exc:
            logger.warning("course_credit_note_failed", subscription_id=str(sub.id), error=str(exc))
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
        cn_number = str((credit_note or {}).get("credit_note_number") or (credit_note or {}).get("name") or "")
        cn_id = str((credit_note or {}).get("credit_note_id") or (credit_note or {}).get("id") or "") or None

        refund = Refund(
            id=uuid.uuid4(),
            refund_number=refund_number,
            payment_id=payment.id if payment is not None else None,
            invoice_number=sub.invoice_number,
            credit_note_number=cn_number or None,
            credit_note_id=cn_id,
            idempotency_key=f"course_subscription_refund:{sub.id}",
            customer_id=sub.customer_id,
            customer_data=sub.customer_data,
            refund_type="manual",
            refund_method=(refund_method or "cash")[:30],
            status="completed",
            requested_amount=amount,
            cancellation_fee=Decimal("0.000"),
            refunded_amount=amount,
            currency=sub.currency or "KWD",
            reason=reason,
            notes=f"Course subscription {sub.subscription_number}",
            processed_by=actor_id,
            processed_by_data=actor_data,
            processed_at=now,
            completed_at=now,
        )
        self._session.add(refund)
        if payment is not None:
            old_status = payment.status
            payment.amount_refunded = amount
            payment.status = "refunded"
            self._session.add(
                PaymentStatusHistory(
                    payment_id=payment.id,
                    old_status=old_status,
                    new_status="refunded",
                    source="course_subscription_refund",
                    reason=reason,
                    correlation_id=str(sub.id),
                    change_by_user=actor_id,
                    change_by_user_data=actor_data,
                )
            )
        sub.status = CourseSubscriptionStatus.REFUNDED.value
        sub.refund_id = refund.id
        sub.refunded_at = now
        sub.refund_reason = reason
        sub.refund_data = {
            "refund_number": refund_number,
            "refund_method": refund_method or "cash",
            "amount": f"{amount:.3f}",
            "credit_note_number": cn_number,
            "credit_note_id": cn_id,
        }
        await self._repo.flush()
        self._emit(
            CourseSubscriptionRefundedEvent(
                **_sub_base_payload(sub),
                course_name=str((sub.course_data or {}).get("name") or ""),
                price=f"{amount:.3f}",
                refund_id=str(refund.id),
                credit_note_number=cn_number,
            ),
            sub,
        )
        return sub

    # ── Redemption (booking integration) ──────────────────────────────────
    async def lock_for_booking(
        self,
        subscription_id: uuid.UUID,
        *,
        customer_id: Any,
        services: list[dict[str, Any]],
        appointment_start: datetime,
        sessions: list[dict[str, Any]] | None = None,
        addons: list[Any] | None = None,
        extra_minutes: int = 0,
        arrangement_type: str | None = None,
    ) -> CourseSubscription:
        """``SELECT … FOR UPDATE`` the subscription and validate (contract E)."""
        sub = await self.get(subscription_id, for_update=True)
        rules.validate_reservation(
            sub,
            customer_id=customer_id,
            services=services,
            appointment_start=appointment_start,
            sessions=sessions,
            addons=addons,
            extra_minutes=extra_minutes,
            arrangement_type=arrangement_type,
        )
        return sub

    async def reserve(self, sub: CourseSubscription, booking: Any) -> CourseSessionUsage:
        """Create the usage of ``booking`` on the LOCKED ``sub`` (idempotent per booking)."""
        existing = await self._repo.get_usage_by_booking(booking.id, for_update=True)
        if existing is not None and existing.status != CourseUsageStatus.RELEASED.value:
            return existing
        numbers = await self._repo.active_session_numbers(sub.id)
        usage = existing or CourseSessionUsage(id=uuid.uuid4(), subscription_id=sub.id, booking_id=booking.id)
        usage.session_number = rules.next_session_number(numbers)
        usage.status = CourseUsageStatus.RESERVED.value
        usage.reserved_at = _utcnow()
        usage.released_at = None
        usage.release_reason = None
        _booking_snapshot(usage, booking)
        if existing is None:
            self._repo.add(usage)
        sub.reserved_sessions = int(sub.reserved_sessions or 0) + 1
        await self._repo.flush()
        booking.course_subscription_id = sub.id
        booking.course_data = booking_course_data(sub, usage)
        self._emit(CourseSubscriptionSessionReservedEvent(**_usage_payload(sub, usage)), sub)
        return usage

    async def _locked_usage(self, booking: Any) -> tuple[CourseSubscription | None, CourseSessionUsage | None]:
        """Lock order: subscription first, then usage (avoids deadlocks with reserve)."""
        usage = await self._repo.get_usage_by_booking(booking.id)
        if usage is None:
            return None, None
        sub = await self._repo.get(usage.subscription_id, for_update=True)
        usage = await self._repo.get_usage_by_booking(booking.id, for_update=True)
        return sub, usage

    async def consume(self, booking: Any, *, reason: str, event_id: str | None = None) -> CourseSessionUsage | None:
        """completed / no_show: reserved → consumed (terminal). Duplicate calls are no-ops."""
        sub, usage = await self._locked_usage(booking)
        if sub is None or usage is None:
            logger.warning("course_usage_missing_on_consume", booking_id=str(booking.id))
            return None
        if usage.status == CourseUsageStatus.CONSUMED.value:
            return usage
        if usage.status == CourseUsageStatus.RELEASED.value:
            # A cancelled session that is completed anyway: take a session again if possible.
            if rules.available_sessions(sub) <= 0:
                logger.error("course_consume_after_release_no_balance", booking_id=str(booking.id))
                return usage
            sub.reserved_sessions = int(sub.reserved_sessions or 0) + 1
        now = _utcnow()
        usage.status = CourseUsageStatus.CONSUMED.value
        usage.consumed_at = now
        usage.consumed_reason = reason
        usage.consumed_by_event_id = event_id
        sub.reserved_sessions = max(int(sub.reserved_sessions or 0) - 1, 0)
        sub.used_sessions = int(sub.used_sessions or 0) + 1
        if sub.status == CourseSubscriptionStatus.ACTIVE.value and sub.used_sessions >= sub.total_sessions:
            sub.status = CourseSubscriptionStatus.EXHAUSTED.value
        await self._repo.flush()
        self._emit(
            CourseSubscriptionSessionConsumedEvent(
                **_usage_payload(sub, usage),
                reason=reason,
                used_sessions=int(sub.used_sessions),
                total_sessions=int(sub.total_sessions),
            ),
            sub,
        )
        return usage

    async def release(self, booking: Any, *, reason: str) -> CourseSessionUsage | None:
        """cancelled / refunded: reserved → released. Consumed stays consumed."""
        sub, usage = await self._locked_usage(booking)
        if sub is None or usage is None:
            return None
        if usage.status != CourseUsageStatus.RESERVED.value:
            return usage
        usage.status = CourseUsageStatus.RELEASED.value
        usage.released_at = _utcnow()
        usage.release_reason = reason
        sub.reserved_sessions = max(int(sub.reserved_sessions or 0) - 1, 0)
        await self._repo.flush()
        self._emit(CourseSubscriptionSessionReleasedEvent(**_usage_payload(sub, usage), reason=reason), sub)
        return usage

    async def reactivate(self, booking: Any) -> CourseSessionUsage | None:
        """A released booking moved back to an active status: reserve again (validated)."""
        sub, usage = await self._locked_usage(booking)
        if sub is None or usage is None or usage.status != CourseUsageStatus.RELEASED.value:
            return usage
        rules.validate_reservation(
            sub,
            customer_id=booking.customer_id,
            services=[{"service_id": str(sub.service_id)}],
            appointment_start=booking.appointment_start,
            arrangement_type=sub.arrangement_type,
        )
        return await self.reserve(sub, booking)

    async def refresh_usage_snapshot(self, booking: Any, *, validate_expiry: bool = False) -> CourseSessionUsage | None:
        """Reschedule / edit: refresh the snapshot; a new start must be < expires_at."""
        sub, usage = await self._locked_usage(booking)
        if sub is None or usage is None:
            return None
        if usage.status != CourseUsageStatus.RESERVED.value:
            return usage
        if validate_expiry:
            rules.assert_start_before_expiry(sub, booking.appointment_start)
        _booking_snapshot(usage, booking)
        await self._repo.flush()
        return usage

    async def validate_booking_edit(self, booking: Any) -> None:
        from app.booking.domain.services_snapshot import get_booking_services

        sid = _uuid(getattr(booking, "course_subscription_id", None))
        if sid is None:
            return
        sub = await self._repo.get(sid)
        if sub is None:
            return
        rules.validate_booking_update(
            sub,
            services=get_booking_services(booking),
            sessions=list(getattr(booking, "sessions", None) or []),
            addons=list(getattr(booking, "addons", None) or []),
            extra_minutes=int(getattr(booking, "extra_minutes", 0) or 0),
        )

    async def on_booking_status(self, booking: Any, new_status: str, *, reason: str | None = None) -> None:
        """Dispatch a booking status change to consume / release / reactivate."""
        status_val = str(getattr(new_status, "value", new_status) or "").lower()
        if status_val in ("completed", "no_show"):
            await self.consume(booking, reason=status_val)
        elif status_val in ("cancelled", "refunded"):
            await self.release(booking, reason=reason or status_val)
        elif status_val in ("confirmed", "checked_in", "in_progress", "reschedule_approved", "requested", "payment_pending"):
            await self.reactivate(booking)

    # ── Background tasks ──────────────────────────────────────────────────
    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """Active subscriptions past ``expires_at`` → expired (+ event). Returns count."""
        now = now or _utcnow()
        subs = await self._repo.claim_due_for_expiry(now, limit)
        for sub in subs:
            lost = rules.available_sessions(sub)
            sub.status = CourseSubscriptionStatus.EXPIRED.value
            self._emit(
                CourseSubscriptionExpiredEvent(
                    **_sub_base_payload(sub),
                    course_name=str((sub.course_data or {}).get("name") or ""),
                    expires_at=_iso(sub.expires_at),
                    lost_sessions=lost,
                ),
                sub,
            )
        if subs:
            await self._repo.flush()
        return len(subs)

    async def retry_failed_invoices(self, *, limit: int = 50) -> int:
        """Retry ushanr invoices of paid subscriptions. Returns the number created."""
        subs = await self._repo.claim_invoice_retries(limit, self._settings.COURSE_INVOICE_MAX_ATTEMPTS)
        created = 0
        for sub in subs:
            if await self._create_invoice(sub):
                created += 1
        return created

    # ── Reads ─────────────────────────────────────────────────────────────
    async def list_usages(self, sub: CourseSubscription) -> list[CourseSessionUsage]:
        return await self._repo.list_usages(sub.id)
