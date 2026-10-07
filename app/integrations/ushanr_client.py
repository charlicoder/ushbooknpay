"""
app/integrations/ushanr_client.py
───────────────────────────────────
Client for the ushanr accounting/invoicing service.

Used by ushbooknpay to fetch or auto-create invoices in ushanr.
Calls internal endpoints directly using a shared X-Internal-Key header.
All calls are non-blocking on error — returning None or empty rather than crashing.
"""
from __future__ import annotations

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
            body = {
                "company_id": company_id,
                "external_id": str(external_id),
                "name": name.strip() or f"Customer {external_id[:8]}",
                "phone": phone.strip() or None,
                "email": email.strip() or None,
                "partner_type": "customer",
            }
            resp = await self._client.post("/api/v1/internal/partners/ensure/", json=body)
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get("partner_id")
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
        """
        # First check if invoice already exists
        existing = await self.get_invoice_by_source("booking", str(booking.id))
        if existing and existing.get("invoice_name"):
            return str(existing["invoice_name"])

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
        service_data = getattr(booking, "service_data", None) or {}
        service_name = service_data.get("name") or "Spa Service"
        total_amount = Decimal(str(getattr(booking, "total_amount", 0) or 0))

        lines = [
            {
                "account_id": revenue_acct,
                "name": service_name,
                "quantity": 1.0,
                "unit_price": float(total_amount),
                "discount": 0.0,
            }
        ]

        booking_number = getattr(booking, "booking_number", None) or ""
        notes = f"Booking {booking_number or booking.id}"
        if service_name:
            notes += f" — {service_name}"

        body = {
            "company_id": company_id,
            "partner_id": partner_id,
            "journal_id": journal_id,
            "invoice_date": date.today().isoformat(),
            "source_document_type": "booking",
            "source_document_id": str(booking.id),
            "source_document_ref": booking_number,
            "currency_code": getattr(booking, "currency", "KWD") or "KWD",
            "notes": notes,
            "lines": lines,
        }

        try:
            resp = await self._client.post("/api/v1/internal/invoices/from-source/", json=body)
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get("invoice_name")
            return None
        except Exception as exc:
            logger.warning("ushanr_create_booking_invoice_failed", booking_id=str(booking.id), error=str(exc))
            return None

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
