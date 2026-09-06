"""
app/clients/ushauth.py
──────────────────────
Async HTTP client for inter-service calls to ushauth.

Currently exposes:
  get_or_create_customer(phone_number, full_name) -> dict
    POST /api/v1/customers/get-or-create/
    Returns the ushauth customer profile dict (id, name, phone_number, email, avatar, created).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


async def get_or_create_customer(
    phone_number: str,
    full_name: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """
    Call ushauth POST /api/v1/customers/get-or-create/ and return the
    customer profile dict.

    Args:
        phone_number: E.164 phone number of the recipient.
        full_name:    Optional display name to pass when creating a new customer.
        settings:     Optional Settings instance (uses get_settings() if omitted).

    Returns:
        Customer profile dict with keys: id, name, phone_number, email, avatar, created.

    Raises:
        httpx.HTTPError: on network or non-2xx HTTP errors (caller decides how to handle).
    """
    if settings is None:
        settings = get_settings()

    url = f"{settings.ushauth_base_url.rstrip('/')}/api/v1/customers/get-or-create/"
    payload: dict[str, Any] = {"phone_number": phone_number}
    if full_name:
        payload["full_name"] = full_name

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-USHSPA-TOKEN": settings.USHSPA_TOKEN,
        "USHSPA-TOKEN": settings.USHSPA_TOKEN,
    }

    async with httpx.AsyncClient(timeout=settings.GATEWAY_TIMEOUT) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()

    body = response.json()
    # ushauth returns { "success": true, "data": { ... } }
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body
