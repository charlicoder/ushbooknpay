"""
app/integrations/ushanr_client.py
───────────────────────────────────
Client for the ushanr accounting/invoicing service.

Used by ushbooknpay to fetch or auto-create invoices in ushanr.
Calls internal endpoints directly using a shared X-Internal-Key header.
All calls are non-blocking on error — returning None or empty rather than crashing.

Accounting note:
  Invoices in ushanr transition through draft → posted → paid.
  When an invoice is paid, its linked general ledger JournalEntry remains 'posted'
  because in standard double-entry bookkeeping, journal entries are immutable
  audit records and do not possess a 'paid' state.
"""
from __future__ import annotations
from app.common.utils import local_now

import logging
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_TIMEOUT = 10.0


class UshanrClient:
    """Async HTTP client for ushanr internal endpoints."""

    def __init__(self) -> None:
        settings = get_settings()
        base_url = (settings.USHANR_BASE_URL or "http://host.docker.internal:8007").rstrip("/")
        app_token = (
            settings.USHSPA_TOKEN.get_secret_value()
            if hasattr(settings.USHSPA_TOKEN, "get_secret_value")
            else str(settings.USHSPA_TOKEN)
        )
        self._api_key = settings.USHANR_INTERNAL_API_KEY or app_token
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=_TIMEOUT,
            headers={
                "X-Internal-Key": self._api_key,
                "Content-Type": "application/json",
            },
        )

    async def get_invoice_by_source(
        self,
        source_type: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        """Fetch invoice dict for a source document from ushanr."""
        try:
            resp = await self._client.get(
                "/api/v1/internal/invoices/by-source/",
                params={
                    "source_document_type": source_type,
                    "source_document_id": str(source_id),
                },
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as exc:
            logger.debug(
                "ushanr_get_invoice_failed",
                source_type=source_type,
                source_id=source_id,
                error=str(exc),
            )
            return None

    async def ensure_partner(
        self,
        *,
        company_id: str,
        external_id: str,
        name: str,
        phone: str = "",
        email: str = "",
    ) -> str | None:
        """Get or create partner record in ushanr."""
        try:
            clean_phone = str(phone or "").strip()
            clean_email = str(email or "").strip()
            clean_name = str(name or "").strip() or f"Customer {str(external_id)[:8]}"
            body = {
                "company_id": str(company_id),
                "external_id": str(external_id),
                "name": clean_name,
                "phone": clean_phone,
                "email": clean_email,
                "partner_type": "customer",
                "currency_code": "KWD",
            }
            resp = await self._client.post("/api/v1/internal/partners/ensure/", json=body)
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get("partner_id")
            logger.warning(
                "ushanr_ensure_partner_non_200",
                status_code=resp.status_code,
                detail=resp.text[:300],
            )
            return None
        except Exception as exc:
            logger.warning("ushanr_ensure_partner_failed", error=str(exc))
            return None

    async def create_or_get_booking_invoice(
        self,
        *,
        booking: Any,
        customer_name: str = "",
        customer_phone: str = "",
        customer_email: str = "",
    ) -> str | None:
        """
        Create (or retrieve if already exists) the ushanr invoice for a booking.
        Returns the invoice_name (e.g. INV/2026/10/00001) or None.
        Only creates invoices when payment is successful (payment_status=success/paid).
        """
        p_status = str(getattr(booking, "payment_status", "") or "").strip().lower()
        if p_status not in ("paid", "success", "completed"):
            return None
        data = await self.create_or_get_booking_invoice_data(
            booking=booking,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        return data.get("invoice_number") if data else None

    async def create_or_get_booking_invoice_data(
        self,
        booking: Any,
        customer_name: str = "",
        customer_phone: str = "",
        customer_email: str = "",
    ) -> dict[str, str] | None:
        """
        Create (or retrieve if already exists) the ushanr invoice for a booking.
        Returns dict with {"invoice_id": str, "invoice_number": str} or None.

        Invoices are ONLY created when payment has been made (payment_status=success/paid).
        Unpaid or pending bookings never create an invoice.
        """
        # First check if invoice already exists
        existing = await self.get_invoice_by_source("booking", str(booking.id))
        if existing and existing.get("invoice_name"):
            return {
                "invoice_id": str(existing.get("invoice_id") or ""),
                "invoice_number": str(existing.get("invoice_name") or ""),
            }

        # Do not create an invoice if payment is not successful
        p_status = str(getattr(booking, "payment_status", "") or "").strip().lower()
        is_paid = p_status in ("paid", "success", "rewarded", "completed")
        if not is_paid:
            logger.info(
                "ushanr_booking_invoice_skipped_unpaid",
                booking_id=str(getattr(booking, "id", "")),
                payment_status=p_status,
                message="Invoice creation skipped because payment_status is not success/paid.",
            )
            return None

        # Check if ushanr invoicing is configured
        company_id = self._settings.USHANR_COMPANY_ID
        journal_id = self._settings.USHANR_AR_JOURNAL_ID
        revenue_acct = self._settings.USHANR_REVENUE_ACCOUNT_ID
        if not (company_id and journal_id and revenue_acct):
            return None

        customer_id = str(getattr(booking, "customer_id", ""))
        partner_id = await self.ensure_partner(
            company_id=company_id,
            external_id=customer_id,
            name=customer_name,
            phone=customer_phone,
            email=customer_email,
        )
        if not partner_id:
            return None

        # Build lines
        from app.booking.domain.services_snapshot import (
            entry_service_name,
            get_booking_services,
            joined_service_name,
            total_services_price,
        )

        service_entries = get_booking_services(booking)
        service_name = joined_service_name(service_entries) or "Spa Service"
        total_amount = Decimal(str(getattr(booking, "total_amount", 0) or 0)).quantize(Decimal("0.001"))
        discount_amount = Decimal(str(getattr(booking, "discount", 0) or 0)).quantize(Decimal("0.001"))
        discount_requested_by = str(getattr(booking, "discount_requested_by", None) or "").strip()
        currency = getattr(booking, "currency", "KWD") or "KWD"

        # Compute pre-discount subtotal for full accounting record
        arr_price = Decimal(str(getattr(booking, "arrangement_price", 0) or 0)).quantize(Decimal("0.001"))
        add_price = Decimal(str(getattr(booking, "addon_price", 0) or 0)).quantize(Decimal("0.001"))
        ext_price = Decimal(str(getattr(booking, "price_for_extra_minutes", 0) or 0)).quantize(Decimal("0.001"))
        fees_price = Decimal(str(getattr(booking, "fees", 0) or 0)).quantize(Decimal("0.001"))
        subtotal_amount = (arr_price + add_price + ext_price + fees_price).quantize(Decimal("0.001"))
        if subtotal_amount <= Decimal("0.000"):
            subtotal_amount = (total_amount + discount_amount).quantize(Decimal("0.001"))

        raw_lines: list[dict[str, Any]] = []
        if service_entries:
            for e in service_entries:
                orig_p = Decimal(
                    str(
                        e.get("original_base_price")
                        if e.get("original_base_price") is not None
                        else (e.get("base_price") or 0)
                    )
                ).quantize(Decimal("0.001"))
                net_p = Decimal(str(e.get("base_price") or orig_p)).quantize(Decimal("0.001"))
                disc_p = Decimal(str(e.get("discount_amount") or 0)).quantize(Decimal("0.001"))
                if orig_p > Decimal("0.000") or net_p > Decimal("0.000"):
                    raw_lines.append({
                        "account_id": revenue_acct,
                        "name": entry_service_name(e) or "Spa Service",
                        "orig_price": orig_p if orig_p > Decimal("0.000") else net_p,
                        "net_price": net_p,
                        "disc_amt": disc_p,
                    })

        booking_addons = getattr(booking, "addons", None) or []
        if isinstance(booking_addons, list) and booking_addons:
            for a in booking_addons:
                if not isinstance(a, dict):
                    continue
                orig_a = Decimal(
                    str(a.get("original_price") if a.get("original_price") is not None else (a.get("price") or 0))
                ).quantize(Decimal("0.001"))
                net_a = Decimal(str(a.get("price") or orig_a)).quantize(Decimal("0.001"))
                disc_a = Decimal(str(a.get("discount_amount") or 0)).quantize(Decimal("0.001"))
                if orig_a > Decimal("0.000") or net_a > Decimal("0.000"):
                    raw_lines.append({
                        "account_id": revenue_acct,
                        "name": f"Add-on: {a.get('name') or 'Add-on'}",
                        "orig_price": orig_a if orig_a > Decimal("0.000") else net_a,
                        "net_price": net_a,
                        "disc_amt": disc_a,
                    })
        elif add_price > Decimal("0.000"):
            raw_lines.append({
                "account_id": revenue_acct,
                "name": "Add-ons",
                "orig_price": add_price,
                "net_price": add_price,
                "disc_amt": Decimal("0.000"),
            })

        if ext_price > Decimal("0.000"):
            ext_mins = int(getattr(booking, "extra_minutes", 0) or 0)
            raw_lines.append({
                "account_id": revenue_acct,
                "name": f"Extra Time ({ext_mins} min)" if ext_mins > 0 else "Extra Time",
                "orig_price": ext_price,
                "net_price": ext_price,
                "disc_amt": Decimal("0.000"),
            })

        if fees_price > Decimal("0.000"):
            raw_lines.append({
                "account_id": revenue_acct,
                "name": "Service Fees",
                "orig_price": fees_price,
                "net_price": fees_price,
                "disc_amt": Decimal("0.000"),
            })

        # Fallback if line items didn't populate
        if not raw_lines:
            raw_lines = [{
                "account_id": revenue_acct,
                "name": service_name,
                "orig_price": subtotal_amount if subtotal_amount > Decimal("0.000") else total_amount,
                "net_price": total_amount,
                "disc_amt": discount_amount,
            }]

        lines: list[dict[str, Any]] = []
        if discount_amount > Decimal("0.000") and subtotal_amount > Decimal("0.000"):
            from app.common.discount_utils import allocate_proportional_amounts

            orig_amounts = [ln["orig_price"] for ln in raw_lines]
            orig_sum = sum(orig_amounts, Decimal("0.000"))
            # Check if native ushanr 2-decimal percentage reconciles cleanly to total_amount
            pct = (discount_amount * Decimal("100") / subtotal_amount).quantize(Decimal("0.01"))
            pct_sum = sum(
                (p * (Decimal("1") - pct / Decimal("100"))).quantize(Decimal("0.001"))
                for p in orig_amounts
            )
            if abs(orig_sum - subtotal_amount) <= Decimal("0.005") and abs(pct_sum - total_amount) <= Decimal("0.001"):
                for ln in raw_lines:
                    lines.append({
                        "account_id": ln["account_id"],
                        "name": ln["name"],
                        "quantity": 1.0,
                        "unit_price": float(ln["orig_price"]),
                        "discount": float(pct),
                    })
            else:
                alloc = allocate_proportional_amounts(orig_amounts, discount_amount)
                for ln, (net_val, disc_val) in zip(raw_lines, alloc):
                    label = ln["name"]
                    if disc_val > Decimal("0.000"):
                        label = f"{label} (Orig: {ln['orig_price']:.3f}, Disc: -{disc_val:.3f} {currency})"
                    lines.append({
                        "account_id": ln["account_id"],
                        "name": label,
                        "quantity": 1.0,
                        "unit_price": float(net_val),
                        "discount": 0.0,
                    })
        else:
            orig_sum = sum((ln["orig_price"] for ln in raw_lines), Decimal("0.000"))
            if abs(orig_sum - total_amount) <= Decimal("0.005"):
                for ln in raw_lines:
                    lines.append({
                        "account_id": ln["account_id"],
                        "name": ln["name"],
                        "quantity": 1.0,
                        "unit_price": float(ln["orig_price"]),
                        "discount": 0.0,
                    })
            else:
                lines = [{
                    "account_id": revenue_acct,
                    "name": service_name,
                    "quantity": 1.0,
                    "unit_price": float(total_amount),
                    "discount": 0.0,
                }]

        booking_number = getattr(booking, "booking_number", None) or ""
        notes = f"Booking {booking_number or booking.id}"
        if service_name:
            notes += f" — {service_name}"
        if discount_amount > Decimal("0.000"):
            req_part = f" (Requested by: {discount_requested_by})" if discount_requested_by else ""
            notes += (
                f" | Subtotal: {subtotal_amount:.3f} {currency}"
                f" | Discount: -{discount_amount:.3f} {currency}{req_part}"
                f" | Final Total: {total_amount:.3f} {currency}"
            )

        body = {
            "company_id": company_id,
            "partner_id": partner_id,
            "journal_id": journal_id,
            "invoice_date": local_now().date().isoformat(),
            "source_document_type": "booking",
            "source_document_id": str(booking.id),
            "source_document_ref": booking_number,
            "currency_code": getattr(booking, "currency", "KWD") or "KWD",
            "notes": notes,
            "lines": lines,
            "is_paid": True,
            "payment_status": p_status or "success",
            "amount_paid": float(total_amount),
            "payment_id": str(getattr(booking, "payment_id", None) or ""),
        }

        try:
            resp = await self._client.post("/api/v1/internal/invoices/from-source/", json=body)
            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "invoice_id": str(data.get("invoice_id") or ""),
                    "invoice_number": str(data.get("invoice_name") or ""),
                }
            logger.warning(
                "ushanr_create_booking_invoice_non_200",
                booking_id=str(booking.id),
                status_code=resp.status_code,
                detail=resp.text[:300],
            )
            return None
        except Exception as exc:
            logger.warning("ushanr_create_booking_invoice_failed", booking_id=str(booking.id), error=str(exc))
            return None

    async def create_or_get_course_subscription_invoice_data(
        self,
        *,
        subscription: Any,
        customer_name: str = "",
        customer_phone: str = "",
        customer_email: str = "",
    ) -> dict[str, str] | None:
        """
        Create (idempotent on source) the paid invoice of a course purchase.

        ``source_document_type="course_subscription"``, one line
        "{course name} ({N} sessions)" at the purchase price, existing revenue
        account (``USHANR_COURSE_REVENUE_ACCOUNT_ID`` overrides when set).
        Returns ``{"invoice_id", "invoice_number"}`` or None on any failure.
        """
        source_type = "course_subscription"
        existing = await self.get_invoice_by_source(source_type, str(subscription.id))
        if existing and existing.get("invoice_name"):
            return {
                "invoice_id": str(existing.get("invoice_id") or ""),
                "invoice_number": str(existing.get("invoice_name") or ""),
            }

        company_id = self._settings.USHANR_COMPANY_ID
        journal_id = self._settings.USHANR_AR_JOURNAL_ID
        revenue_acct = (
            getattr(self._settings, "USHANR_COURSE_REVENUE_ACCOUNT_ID", "") or self._settings.USHANR_REVENUE_ACCOUNT_ID
        )
        if not (company_id and journal_id and revenue_acct):
            logger.warning("ushanr_course_invoice_not_configured", subscription_id=str(subscription.id))
            return None

        partner_id = await self.ensure_partner(
            company_id=company_id,
            external_id=str(subscription.customer_id),
            name=customer_name,
            phone=customer_phone,
            email=customer_email,
        )
        if not partner_id:
            return None

        course = subscription.course_data or {}
        course_name = str(course.get("name") or course.get("name_en") or course.get("code") or "Course")
        total_amount = Decimal(str(subscription.price or 0))
        number = getattr(subscription, "subscription_number", None) or ""
        body = {
            "company_id": company_id,
            "partner_id": partner_id,
            "journal_id": journal_id,
            "invoice_date": local_now().date().isoformat(),
            "source_document_type": source_type,
            "source_document_id": str(subscription.id),
            "source_document_ref": number,
            "currency_code": getattr(subscription, "currency", "KWD") or "KWD",
            "notes": f"Course subscription {number} — {course_name}",
            "lines": [
                {
                    "account_id": revenue_acct,
                    "name": f"{course_name} ({int(subscription.total_sessions or 0)} sessions)",
                    "quantity": 1.0,
                    "unit_price": float(total_amount),
                    "discount": 0.0,
                }
            ],
            "is_paid": True,
            "payment_status": "success",
            "amount_paid": float(total_amount),
            "payment_id": str(getattr(subscription, "payment_id", None) or ""),
        }
        try:
            resp = await self._client.post("/api/v1/internal/invoices/from-source/", json=body)
            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "invoice_id": str(data.get("invoice_id") or ""),
                    "invoice_number": str(data.get("invoice_name") or ""),
                }
            logger.warning(
                "ushanr_create_course_invoice_non_200",
                subscription_id=str(subscription.id),
                status_code=resp.status_code,
                detail=resp.text[:300],
            )
            return None
        except Exception as exc:
            logger.warning("ushanr_create_course_invoice_failed", subscription_id=str(subscription.id), error=str(exc))
            return None

    async def mark_invoice_paid(
        self,
        *,
        invoice_id: str | None = None,
        invoice_name: str | None = None,
        source_document_type: str | None = None,
        source_document_id: str | None = None,
        source_document_ref: str | None = None,
        amount_paid: float | Decimal | None = None,
        payment_id: str | None = None,
    ) -> bool:
        """Mark an invoice as paid in ushanr."""
        body: dict[str, Any] = {}
        if invoice_id:
            body["invoice_id"] = str(invoice_id)
        if invoice_name:
            body["invoice_name"] = str(invoice_name)
        if source_document_type and source_document_id:
            body["source_document_type"] = str(source_document_type)
            body["source_document_id"] = str(source_document_id)
        if source_document_ref:
            body["source_document_ref"] = str(source_document_ref)
        if amount_paid is not None:
            body["amount_paid"] = float(amount_paid)
        if payment_id:
            body["payment_id"] = str(payment_id)
        try:
            resp = await self._client.post("/api/v1/internal/invoices/mark-paid/", json=body)
            return resp.status_code in (200, 201)
        except Exception as exc:
            logger.debug("ushanr_mark_invoice_paid_failed", error=str(exc))
            return False

    async def create_credit_note(
        self,
        *,
        source_document_type: str,
        source_document_id: str,
        cancellation_fee: Decimal | float | None = None,
        refund_amount: Decimal | float | None = None,
        refund_method: str | None = None,
        refund_number: str | None = None,
        branch_id: str | None = None,
        processed_by: str | None = None,
        record_refund_payment: bool = False,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Create or update credit note in ushanr (e.g. on refund approval or completion)."""
        company_id = self._settings.USHANR_COMPANY_ID
        journal_id = self._settings.USHANR_AR_JOURNAL_ID
        if not (company_id and journal_id):
            return None
        body = {
            "company_id": company_id,
            "journal_id": journal_id,
            "source_document_type": source_document_type,
            "source_document_id": str(source_document_id),
            "cancellation_date": local_now().date().isoformat(),
            "cancellation_fee": float(cancellation_fee) if cancellation_fee is not None else None,
            "refund_amount": float(refund_amount) if refund_amount is not None else None,
            "refund_method": refund_method or "cash",
            "refund_number": refund_number,
            "branch_id": str(branch_id) if branch_id else None,
            "processed_by": processed_by or "ushbooknpay",
            "record_refund_payment": record_refund_payment,
            "notes": notes,
        }
        try:
            resp = await self._client.post("/api/v1/internal/invoices/credit-note/", json=body)
            if resp.status_code in (200, 201):
                return resp.json()
            logger.warning(
                "ushanr_create_credit_note_non_2xx",
                status_code=resp.status_code,
                source_document_type=source_document_type,
                source_document_id=str(source_document_id),
                response_text=resp.text[:300],
            )
            return None
        except Exception as exc:
            logger.warning("ushanr_create_credit_note_failed", error=str(exc))
            return None

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
